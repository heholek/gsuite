# -*- coding: utf-8 -*-
#
# Univention Google Apps for Work - listener module to provision accounts in
# Google Directory
#
# Copyright 2016 Univention GmbH
#
# http://www.univention.de/
#
# All rights reserved.
#
# The source code of this program is made available
# under the terms of the GNU Affero General Public License version 3
# (GNU AGPL V3) as published by the Free Software Foundation.
#
# Binary versions of this program provided by Univention to you as
# well as other copyrighted, protected or trademarked materials like
# Logos, graphics, fonts, specific documentations and configurations,
# cryptographic keys etc. are subject to a license agreement between
# you and Univention and not subject to the GNU AGPL V3.
#
# In the case you use this program under the terms of the GNU AGPL V3,
# the program is provided in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License with the Debian GNU/Linux or Univention distribution in file
# /usr/share/common-licenses/AGPL-3; if not, see
# <http://www.gnu.org/licenses/>.


__package__ = ''  # workaround for PEP 366

import os
import re
try:
	import cPickle as pickle
except ImportError:
	# py3
	import pickle
import json
import base64
import zlib
import copy
from stat import S_IRUSR, S_IWUSR
import datetime
import grp

import listener
from univention.googleapps.auth import GappsAuth
from univention.googleapps.listener import GoogleAppsListener
from univention.googleapps.logging2udebug import get_logger


# attributes that should be anonymized (google-apps/attributes/anonymize):
attributes_anonymize = list()
# template of user resource, constructed from UCRVs:
g_user_resource_template = dict()
# attributes that should not be synced (google-apps/attributes/never):
attributes_never = list()
# mapping from UCS LDAP attributes to properties in the template:
ldap2google = dict()
# user is not allowed to set these:
google_attributes_blacklisted = ["univentionGoogleAppsObjectID", "univentionGoogleAppsData", "kind", "id", "etag",
	"isAdmin", "isDelegatedAdmin", "lastLoginTime", "creationTime", "deletionTime", "agreedToTerms", "password",
	"hashFunction", "suspended", "suspensionReason", "changePasswordAtNextLogin", "ipWhitelisted", "nonEditableAliases",
	"customerId", "isMailboxSetup", "thumbnailPhotoEtag"]
# used to create a correct user resource:
# * check user supplied properties
# * set the correct object type for each property
google_user_property_types = dict(addresses=list, agreedToTerms=bool, aliases=list, changePasswordAtNextLogin=bool,
	creationTime=datetime.datetime, customSchemas=dict, customerId=unicode, deletionTime=datetime.datetime,
	emails=list, etag=unicode, externalIds=list, hashFunction=unicode, id=unicode, ims=list,
	includeInGlobalAddressList=bool, ipWhitelisted=bool, isAdmin=bool, isDelegatedAdmin=bool,
	isMailboxSetup=bool, kind=unicode, lastLoginTime=datetime.datetime, name=dict, nonEditableAliases=list,
	notes=dict, orgUnitPath=unicode, organizations=list, password=unicode, phones=list, primaryEmail=unicode,
	relations=list, suspended=bool, suspensionReason=unicode, thumbnailPhotoEtag=unicode, thumbnailPhotoUrl=unicode,
	websites=list)
# required properties in nested structures in user resource:
required_properties = dict(
	emails=["address"],
	externalIds=["value"],
	ims=["im"],
	name=["familyName", "givenName"],
	notes=["value"],
	organizations=["name"],
	phones=["value"],
	relations=["value"],
	websites=["value"]
)
# Requirement for "name" in "organizations" isn't by google.
# It simply doesn't make any sense without it and it would create strange
# empty entries with the default UCRVs.

logger = get_logger("google-apps", "gafw")

listener.configRegistry.load()


def get_listener_attributes():
	global attributes_anonymize, g_user_resource_template, attributes_never, ldap2google

	# blacklisted > never > anonymize > static > sync
	ucr_never = listener.configRegistry.get("google-apps/attributes/never", "")
	attributes_never.extend([x.strip() for x in ucr_never.split(",") if x.strip()])

	ucr_anon = listener.configRegistry.get("google-apps/attributes/anonymize", "")
	attributes_anonymize = [x.strip() for x in ucr_anon.split(",")
		if x.strip() and x not in google_attributes_blacklisted and x not in attributes_never]

	for k, v in listener.configRegistry.items():
		if k.startswith("google-apps/attributes/mapping/"):
			m = re.match(r"^google-apps/attributes/mapping/(.*?)/.*$", k)
			if not m:
				m = re.match(r"^google-apps/attributes/mapping/(.*?)$", k)
			google_attrib = m.groups()[0]

			if google_attrib in google_attributes_blacklisted:
				logger.warn("Ignoring blacklisted google directory user property %r.", google_attrib)
				continue
			if google_attrib not in google_user_property_types:
				logger.warn("Ignoring unknown google directory user property %r.", google_attrib)
				continue

			v = v.strip()
			if not v:
				continue

			vals = v.split(",")
			if len(vals) > 1:
				gprop = dict()
				for prop in vals:
					gk, _, action = prop.partition("=")
					gprop[gk] = action
			else:
				gprop = v

			if google_user_property_types[google_attrib] == list:
				try:
					g_user_resource_template[google_attrib].append(gprop)
				except KeyError:
					g_user_resource_template[google_attrib] = [gprop]
			else:
				g_user_resource_template[google_attrib] = gprop

			for prop in v.split(","):
				gk, _, action = prop.partition("=")
				if not action:
					# single value
					action = gk
				if action.startswith("%"):
					ldap_attr = action[1:]
					if ldap_attr not in attributes_never:
						try:
							ldap2google[ldap_attr].append(google_attrib)
						except KeyError:
							ldap2google[ldap_attr] = [google_attrib]
				else:
					# static -> ignore: will be set just once, when creating the user
					pass
		else:
			pass

	# just for log readability
	attributes_anonymize.sort()
	attributes_never.sort()

	attrs = {"univentionGoogleAppsEnabled"}
	attrs.update(ldap2google.keys())
	return sorted(list(attrs))


name = 'google-apps-user'
description = 'sync users to Google Directory'
filter = '(&(objectClass=univentionGoogleApps)(uid=*))' if GappsAuth.is_initialized() else '(foo=bar)'
attributes = get_listener_attributes()
modrdn = "1"

GOOGLEAPPS_USER_OLD_PICKLE = os.path.join("/var/lib/univention-google-apps", "google-apps-user_old_dn")

_attrs = dict(
	anonymize=attributes_anonymize,
	listener=copy.deepcopy(attributes),  # when handler() runs, all kinds of stuff is suddenly in attributes
	template=g_user_resource_template,
	never=attributes_never,
	google_attribs=ldap2google,
	blacklisted=google_attributes_blacklisted,
	google_types=google_user_property_types,
	required_properties=required_properties
)

ldap_cred = dict()

logger.info("listener observing attributes: %r", attributes)
logger.info("user ressource template: %r", g_user_resource_template)
logger.info("attributes to sync anonymized: %r", attributes_anonymize)
logger.info("attributes to never sync: %r", attributes_never)
logger.info("ldap2google attribute triggers: %r", ldap2google)


def load_old(old):
	if os.path.exists(GOOGLEAPPS_USER_OLD_PICKLE):
		f = open(GOOGLEAPPS_USER_OLD_PICKLE, "r")
		p = pickle.Unpickler(f)
		old = p.load()
		f.close()
		os.unlink(GOOGLEAPPS_USER_OLD_PICKLE)
		return old
	else:
		return old


def save_old(old):
	f = open(GOOGLEAPPS_USER_OLD_PICKLE, "w+")
	os.chmod(GOOGLEAPPS_USER_OLD_PICKLE, S_IRUSR | S_IWUSR)
	p = pickle.Pickler(f)
	p.dump(old)
	p.clear_memo()
	f.close()


def setdata(key, value):
	global ldap_cred
	ldap_cred[key] = value


def initialize():
	if not GappsAuth.is_initialized():
		raise RuntimeError("Google Apps for Work App not initialized yet, please run wizard.")


def clean():
	"""
	Remove  univentionGoogleAppsObjectID and univentionGoogleAppsData from all
	user objects.
	"""
	logger.info("clean() removing Google Apps for Work ObjectID and Data from all users.")
	GoogleAppsListener.clean_udm_objects("users/user", listener.configRegistry["ldap/base"], ldap_cred)


def handler(dn, new, old, command):
	logger.debug("command: %s", command)

	if not GappsAuth.is_initialized():
		# TODO: store [dn] = action
		raise RuntimeError("{}.handler() Google Apps for Work App not initialized yet, please run wizard.".format(name))
	else:
		# TODO: replay postponed actions
		pass

	if command == 'r':
		save_old(old)
		return
	elif command == 'a':
		old = load_old(old)

	ol = GoogleAppsListener(listener, _attrs, ldap_cred)

	old_enabled = bool(int(old.get("univentionGoogleAppsEnabled", ["0"])[0]))  # "" when disabled, "1" when enabled
	new_enabled = bool(int(new.get("univentionGoogleAppsEnabled", ["0"])[0]))

	#
	# NEW or REACTIVATED account
	#
	if new_enabled and not old_enabled:
		logger.debug("new_enabled and not old_enabled -> NEW or REACTIVATED (%r)", dn)
		new_user = ol.create_google_user(new)
		# save/update google objectId and object data in UDM object
		udm_user = ol.get_udm_user(dn)
		udm_user["UniventionGoogleAppsObjectID"] = new_user["id"]
		udm_user["UniventionGoogleAppsData"] = base64.encodestring(zlib.compress(json.dumps(new_user)))
		udm_user.modify()
		logger.info("Added google account of user %r. primaryEmail: %r id: %r", new["uid"][0],
			new_user["primaryEmail"], new_user["id"])
		return

	#
	# DELETE account
	#
	if old and not new:
		logger.debug("old and not new -> DELETE (%r)", dn)
		try:
			ol.delete_google_user(old["univentionGoogleAppsObjectID"][0])
			logger.info("Deleted google account of user %r.", old["uid"][0])
		except KeyError:
			pass
		logger.debug("done (%s)", dn)
		return

	#
	# DEACTIVATE account
	#
	if new and not new_enabled:
		logger.debug("new and not new_enabled -> DEACTIVATE (%r)", dn)
		ol.delete_google_user(old["univentionGoogleAppsObjectID"][0])
		# update google objectId and object data in UDM object
		udm_user = ol.get_udm_user(dn)
		# Cannot delete UniventionGoogleAppsData, because it would result in:
		# ldapError: Inappropriate matching: modify/delete: univentionGoogleAppsData: no equality matching rule
		# Explanation: http://gcolpart.evolix.net/blog21/delete-facsimiletelephonenumber-attribute/
		udm_user["UniventionGoogleAppsData"] = base64.encodestring(zlib.compress(json.dumps(None)))
		udm_user.modify()
		username = old["uid"][0]
		logger.info("Deleted google account of user %r.", username)

		filter_s = "({})".format("|".join("(cn={})".format(g.gr_name) for g in grp.getgrall() if username in g.gr_mem))
		base = listener.configRegistry["ldap/base"]
		udm_groups = ol.find_udm_objects("groups/group", filter_s, base, ldap_cred)
		logger.debug("Looking for empty groups to delete...")
		for udm_group in udm_groups:
			try:
				group_id = udm_group["UniventionGoogleAppsObjectID"]
				ol.delete_google_group_if_empty(udm_group["entryDN"], group_id)
			except KeyError:
				pass

		return

	#
	# MODIFY account
	#
	if old_enabled and new_enabled:
		logger.debug("old_enabled and new_enabled -> MODIFY (%r)", dn)
		ol.modify_google_user(old, new)
		# update google object data in UDM object
		udm_user = ol.get_udm_user(dn)
		google_user = ol.get_google_user(new)
		udm_user["UniventionGoogleAppsObjectID"] = google_user["id"]
		udm_user["UniventionGoogleAppsData"] = base64.encodestring(zlib.compress(json.dumps(google_user)))
		udm_user.modify()
		logger.info("Modified google account of user %r.", old["uid"][0])
		return