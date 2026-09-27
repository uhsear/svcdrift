#!/usr/bin/env python
"""Diff a published feature service against the dataset it was published from, and exit non-zero rather than call two different schemas the same.

A feature service is a copy of a schema, not a view of one. Publishing takes a
snapshot of the fields, the aliases, the domains and the spatial reference, and
from that moment the two drift apart in silence. The nightly load keeps writing
the source, every log stays green, and nothing anywhere compares the two. This
tool reads a layer definition over ?f=json, compares it field by field against
another service, a saved JSON snapshot or an arcpy-read feature class, and
exits non-zero when the difference is one that breaks something.

ArcGIS Pro's Compare Schema is the tool people reach for and it is good: it
reports field, index, subtype and domain differences between two geodatabases
in a report you can hand to somebody. It compares geodatabases. It has no
notion of a published service, and in a local government half the schema that
matters lives in the services, behind a REST endpoint that Compare Schema
cannot open. That is the gap. This tool reads the published side over HTTP and
compares it against whichever side you still have.

Severity is the other half. A removed field is a break: a popup, a filter and
a symbology rule all stop working. An added field is a warning, and an alias
change is a warning, because nothing stops. A run that prints twenty alias
changes and one removed field must fail on the removed field and only on it.

The renderer drifts the same way, and a schema check passes it. When both
sides carry a drawingInfo, the renderer, its classes, its symbols, the label
classes and the visibility range are compared after both are reduced to one
canonical form, so a republish that only reorders the JSON, drops a colour's
alpha or adds float noise is not drift. A moved class break, a lost class or
a class drawn in another colour is a break. --schema-only leaves all of it
out, which is how version 1.0 compared.

It is read-only. Nothing it does changes a service, and the only thing it
writes is a snapshot file, which needs --apply.

    python svcdrift.py --self-test
    python svcdrift.py --service https://gis.county.org/server/rest/services/Parcels/FeatureServer/0 \
        --source parcels_baseline.json
    python svcdrift.py --service .../FeatureServer/0 --source C:/data/parcels.gdb/Parcels
    python svcdrift.py --service .../FeatureServer/0 --source .../Other/FeatureServer/0 --strict
    python svcdrift.py --service .../FeatureServer/0 --probe
    python svcdrift.py --service .../FeatureServer/0 --source baseline.json --schema-only
    python svcdrift.py --service .../FeatureServer/0 --out baseline.json --apply

Exit codes: 0 no breaking difference, 1 a breaking difference (or any
difference under --strict), 2 a side could not be read, 64 usage error.
"""

from __future__ import print_function

import argparse
import collections
import datetime
import http.client
import io
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# =============================================================================
# CONFIGURATION. Deliberately not constants at the call site.
# =============================================================================

VERSION = "1.1"

# Seconds before a request to a service is abandoned. A layer definition is a
# small document, but an Enterprise server under a cache rebuild can take most
# of a minute to answer the first call of the day.
HTTP_TIMEOUT = 60

# The longest --timeout accepted. A socket refuses a timeout larger than the
# platform's clock holds, and an infinite one raised an OverflowError that
# exited 1, the code for a break. A day is longer than any layer takes.
MAX_TIMEOUT = 86400

# What replaces a token anywhere it could otherwise be printed. urllib puts the
# url it could not open into its own error message, and that url carries the
# token as a query parameter, so every error text goes through redact().
REDACTED = "[redacted]"

# Environment variable the token may arrive in, so that a scheduled task does
# not have to put it on a command line where every process on the box can read
# it. A --token flag is still accepted, because an interactive run is a
# different threat model from a cron entry.
TOKEN_ENV = "SVCDRIFT_TOKEN"

# The token for a --source service, also accepted as --source-token. Without
# one, a --source on the --service host gets the --service token and one on
# another host gets none, because a token goes only to the host it is for.
SOURCE_TOKEN_ENV = "SVCDRIFT_SOURCE_TOKEN"

# The two severities. A break stops something working; a warning does not.
BREAK = "BREAK"
WARNING = "WARNING"

SEVERITY_RANK = {BREAK: 0, WARNING: 1}

# Field types the REST API reports. Listed so that a name arriving in a
# different case is still recognised as the same type.
ESRI_TYPES = (
    "esriFieldTypeOID", "esriFieldTypeGlobalID", "esriFieldTypeGUID",
    "esriFieldTypeString", "esriFieldTypeSmallInteger", "esriFieldTypeInteger",
    "esriFieldTypeBigInteger", "esriFieldTypeSingle", "esriFieldTypeDouble",
    "esriFieldTypeDate", "esriFieldTypeDateOnly", "esriFieldTypeTimeOnly",
    "esriFieldTypeTimestampOffset", "esriFieldTypeGeometry",
    "esriFieldTypeBlob", "esriFieldTypeRaster", "esriFieldTypeXML",
)

# What arcpy calls the same types. arcpy.ListFields gives "Integer" where the
# REST API gives "esriFieldTypeInteger", and a comparison that does not fold
# these together reports a type change on every single field of every dataset
# to service comparison, which is the whole tool made useless.
ARCPY_TYPES = {
    "OID": "esriFieldTypeOID",
    "GLOBALID": "esriFieldTypeGlobalID",
    "GUID": "esriFieldTypeGUID",
    "STRING": "esriFieldTypeString",
    "TEXT": "esriFieldTypeString",
    "SMALLINTEGER": "esriFieldTypeSmallInteger",
    "SHORT": "esriFieldTypeSmallInteger",
    "INTEGER": "esriFieldTypeInteger",
    "LONG": "esriFieldTypeInteger",
    "BIGINTEGER": "esriFieldTypeBigInteger",
    "SINGLE": "esriFieldTypeSingle",
    "FLOAT": "esriFieldTypeSingle",
    "DOUBLE": "esriFieldTypeDouble",
    "DATE": "esriFieldTypeDate",
    "DATEONLY": "esriFieldTypeDateOnly",
    "TIMEONLY": "esriFieldTypeTimeOnly",
    "TIMESTAMPOFFSET": "esriFieldTypeTimestampOffset",
    "GEOMETRY": "esriFieldTypeGeometry",
    "SHAPE": "esriFieldTypeGeometry",
    "BLOB": "esriFieldTypeBlob",
    "RASTER": "esriFieldTypeRaster",
    "XML": "esriFieldTypeXML",
}

TYPE_ALIASES = dict((name.upper(), name) for name in ESRI_TYPES)
TYPE_ALIASES.update(ARCPY_TYPES)

# The two field types that carry a ROLE rather than data. A service names its
# object id field whatever the publish gave it, and the source may call the
# same column FID or OBJECTID_1. Matched by name they read as one field removed
# and one field added, on every comparison ever run.
IDENTITY_ROLES = {
    "esriFieldTypeOID": "oid",
    "esriFieldTypeGlobalID": "globalid",
}

# Type changes that lose nothing. A short integer column republished as a long
# still holds every value it held, so it is a warning rather than a break. The
# ladders do not cross: an integer to a double is a real break, because a client
# that parses integers has to be changed.
WIDENING_LADDERS = (
    ("esriFieldTypeSmallInteger", "esriFieldTypeInteger",
     "esriFieldTypeBigInteger"),
    ("esriFieldTypeSingle", "esriFieldTypeDouble"),
)

# Only a text field has a length worth comparing. arcpy reports length 4 for a
# long and 8 for a date, the REST API reports those inconsistently or not at
# all, and comparing them produces a length change on numeric fields nobody
# touched.
LENGTH_TYPES = frozenset(["esriFieldTypeString"])

# Well known ids that mean the same spatial reference. 102100 is the old Esri
# code for what is now EPSG 3857, and a service that reports one against a
# source that reports the other is not reprojected, it is spelled differently.
WKID_ALIASES = {102100: 3857, 102113: 3857}

# What arcpy's Describe.shapeType is called in a layer definition.
ARCPY_GEOMETRY = {
    "POINT": "esriGeometryPoint",
    "MULTIPOINT": "esriGeometryMultipoint",
    "POLYLINE": "esriGeometryPolyline",
    "POLYGON": "esriGeometryPolygon",
    "MULTIPATCH": "esriGeometryMultiPatch",
}

# Layer properties compared straight, with the severity a change carries.
# geometryType and spatialReference break every client that draws the layer.
# maxRecordCount changes how much comes back per page, which is a paging loop's
# problem and not a schema break.
PLAIN_PROPERTIES = (
    ("geometryType", "GEOMETRY_TYPE_CHANGED", BREAK),
    ("spatialReference", "SPATIAL_REF_CHANGED", BREAK),
    ("maxRecordCount", "MAX_RECORD_COUNT_CHANGED", WARNING),
    ("subtypes", "SUBTYPES_CHANGED", WARNING),
)

# Two numbers in a drawingInfo are one number when they differ by no more than
# NUMBER_TOLERANCE of the larger one, or by no more than ABSOLUTE_TOLERANCE
# outright. That is math.isclose with those two tolerances. A republish writes
# a class break of 1234.56 back as 1234.5600000000001, which is noise in the
# sixteenth digit, about 1e-16 of the number. A relative 1e-12 takes that out
# and keeps an edit of one unit up to 1e12: a break edited from 10000000000 to
# 10000000009 moved. An earlier relative 1e-9 read that edit as noise. The
# absolute tolerance only matters below 1000, where it is the larger of the
# two. The rule is the same for an integer and a float.
NUMBER_TOLERANCE = 1e-12
ABSOLUTE_TOLERANCE = 1e-9

# Keys that record how a renderer was made rather than what it draws. ArcGIS
# Pro writes authoringInfo when a map is saved and a server may keep it or
# drop it on a publish, and neither changes one pixel. The breaks themselves
# are compared, so the name of the method that chose them is not.
IGNORED_DRAWING_KEYS = frozenset(["authoringInfo", "classificationMethod"])

# Keys a server fills in with their default when the publisher left them out.
# Each is dropped when it holds exactly this value, of exactly this type, so a
# side that spelled the default out reads the same as a side that did not.
DRAWING_DEFAULTS = {
    "angle": 0, "xoffset": 0, "yoffset": 0, "transparency": 0,
    "minScale": 0, "maxScale": 0,
    "kerning": True, "rightToLeft": False,
    "decoration": "none", "style": "normal", "weight": "normal",
    "label": "", "description": "", "defaultLabel": "",
    "where": "", "labelExpression": "", "valueExpression": "",
}

# Keys that hold an expression. An editor adds a trailing newline or turns
# \n into \r\n on the way through, and neither is a different expression.
EXPRESSION_KEYS = frozenset(["expression", "valueExpression",
                             "labelExpression", "where"])

# What a renderer reads a value out of. A change to any of these moves every
# feature into a different class, so it is reported once, as that.
RENDERER_FIELD_KEYS = ("field", "field1", "field2", "field3",
                       "normalizationField")
RENDERER_RULE_KEYS = ("valueExpression", "normalizationType",
                      "normalizationTotal")

# Renderer keys compared by a rule of their own. Every other key is compared
# as plain JSON, so a renderer property this file has never heard of is still
# compared rather than skipped.
RENDERER_HANDLED_KEYS = frozenset(
    RENDERER_FIELD_KEYS + RENDERER_RULE_KEYS
    + ("type", "fieldDelimiter", "uniqueValueInfos", "uniqueValueGroups",
       "classBreakInfos", "minValue", "symbol", "defaultSymbol",
       "visualVariables", "attributes"))

# The keys of a colorInfo visual variable that say which colour a value is
# drawn in. On a map coloured by a ramp the ramp is the classification, so a
# change to one of these is a break, as a class symbol is. A size, opacity or
# rotation ramp that changed still draws a larger value larger, so it is not.
COLOUR_RAMP_KEYS = ("stops", "colors", "minDataValue", "maxDataValue")

# Keys of one class that are its value, its bounds or its symbol. What is left
# of a class after these is its legend text.
CLASS_KEYS = frozenset(["value", "values", "symbol", "classMinValue",
                        "classMaxValue"])

# =============================================================================
# End of CONFIGURATION.
# =============================================================================

# One difference. kind is machine readable, target names the field or property
# it is about, and detail is the sentence a person reads.
Difference = collections.namedtuple("Difference",
                                    "kind severity target detail")


# ----------------------------------------------------------------- pure core

def canonical_type(raw):
    """One spelling for a field type, whoever spelled it.

    An unknown name is returned as it arrived rather than rejected. A service
    on a version newer than this file reports types this table has never heard
    of, and an unknown name compared against itself is still equal, which is
    the answer that does not invent a difference.
    """
    if not isinstance(raw, str):
        raise ValueError("a field type must be a string, got %r" % (raw,))
    name = raw.strip()
    if not name:
        raise ValueError("a field with an empty type")
    return TYPE_ALIASES.get(name.upper(), name)


def field_role(field_type):
    """"oid", "globalid", or "" for a field that carries data."""
    return IDENTITY_ROLES.get(field_type, "")


def is_widening(old, new):
    """True when new holds every value old held, so the change loses nothing."""
    if old == new:
        return False
    for ladder in WIDENING_LADDERS:
        if old in ladder and new in ladder:
            return ladder.index(new) > ladder.index(old)
    return False


def domain_key(domain):
    """A comparable summary of a field domain, or None for no domain.

    arcpy hands back the domain NAME and nothing else, while the REST API hands
    back the whole coded value list. domain_change() knows that, and compares
    only what both sides actually know.
    """
    if domain is None or domain == "" or domain == {}:
        return None
    if isinstance(domain, str):
        return ("name", domain.strip(), ())
    if not isinstance(domain, dict):
        raise ValueError("a domain must be an object or a name, got %r"
                         % (domain,))
    name = "%s" % (domain.get("name") or "")
    kind = "%s" % (domain.get("type") or "")
    if domain.get("codedValues") is not None or kind == "codedValue":
        # Every other rejection in this file is a ValueError, which main()
        # turns into an exit code. A codedValues that is not a list, or a coded
        # value that is not an object, used to come out as a TypeError or an
        # AttributeError traceback, and a traceback exits 1, the break code.
        values = as_list(domain.get("codedValues"), "codedValues")
        for cv in values:
            if not isinstance(cv, dict):
                raise ValueError("a coded value must be an object, got %r"
                                 % (cv,))
        codes = tuple(sorted(
            "%s=%s" % (cv.get("code"), cv.get("name")) for cv in values))
        return ("codedValue", name, codes)
    if domain.get("range") is not None or kind == "range":
        return ("range", name,
                tuple(as_list(domain.get("range"), "a range domain's range")))
    return (kind or "unknown", name, ())


def domain_change(old, new):
    """None, "added", "removed" or "changed" for a pair of domain keys.

    Two domains with the same name are treated as the same domain when either
    side only knows the name. That is the arcpy case: refusing to fold them
    together reports a domain change on every field that has one.
    """
    if old is None and new is None:
        return None
    if old is None:
        return "added"
    if new is None:
        return "removed"
    if old == new:
        return None
    if "name" in (old[0], new[0]):
        return None if old[1] == new[1] else "changed"
    return "changed"


def sr_key(spatial_reference):
    """A comparable spatial reference, or None when the side does not say.

    latestWkid is preferred over wkid, because a service reports 102100 and
    3857 together for web mercator and only the second one means anything to
    anybody else.
    """
    sr = spatial_reference
    if sr is None or sr == "" or sr == {}:
        return None
    if isinstance(sr, bool):
        raise ValueError("a spatial reference of %r" % (sr,))
    if isinstance(sr, int):
        # The same rule the object below is held to: a wkid of 0 is the server
        # saying it does not know, not a spatial reference to compare against.
        return ("wkid", WKID_ALIASES.get(sr, sr)) if sr else None
    if isinstance(sr, str):
        # And a wkt of nothing but whitespace is not a wkt. Left unguarded it
        # compares unequal to the side that reported nothing at all.
        return ("wkt", " ".join(sr.split())) if sr.strip() else None
    if not isinstance(sr, dict):
        raise ValueError("a spatial reference must be an object, a wkid or a "
                         "wkt, got %r" % (sr,))
    for key in ("latestWkid", "wkid"):
        value = sr.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value:
            return ("wkid", WKID_ALIASES.get(value, value))
    for key in ("wkt", "latestWkt"):
        value = sr.get(key)
        if isinstance(value, str) and value.strip():
            return ("wkt", " ".join(value.split()))
    return None


def subtype_key(raw):
    """The subtype field and its codes, or None when the layer has none.

    The REST API calls them types and names the field subtypeField; a
    geodatabase calls the same thing subtypes. Both shapes are read.
    """
    field = (raw.get("subtypeField") or raw.get("subtypeFieldName") or "")
    field = ("%s" % field).strip().upper()
    codes = []
    for entry in as_list(raw.get("types") or raw.get("subtypes"), "types"):
        if not isinstance(entry, dict):
            raise ValueError("a subtype must be an object, got %r" % (entry,))
        code = entry.get("id")
        if code is None:
            code = entry.get("code")
        codes.append(("%s" % code, "%s" % (entry.get("name") or "")))
    if not field and not codes:
        return None
    return (field, tuple(sorted(codes)))


def normalize_field(raw):
    """One field reduced to the properties a drift shows up in."""
    if not isinstance(raw, dict):
        raise ValueError("a field must be an object, got %r" % (raw,))
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("a field with no name: %r" % (raw,))
    name = name.strip()
    ftype = canonical_type(raw.get("type"))
    alias = raw.get("alias")
    if not isinstance(alias, str) or not alias.strip():
        # A side that does not report an alias is not a side whose aliases all
        # changed. arcpy sets aliasName to the field name when none was given,
        # and this matches that rather than inventing an empty one.
        alias = raw.get("aliasName")
    if not isinstance(alias, str) or not alias.strip():
        alias = name
    length = raw.get("length")
    if not isinstance(length, int) or isinstance(length, bool) or length <= 0:
        length = None
    if ftype not in LENGTH_TYPES:
        length = None
    return {
        "name": name,
        "type": ftype,
        "role": field_role(ftype),
        "alias": alias.strip(),
        "length": length,
        "domain": domain_key(raw.get("domain")),
    }


def normalize_layer(raw, label="", symbology=True):
    """A layer definition reduced to what is worth comparing.

    A property the side does not report is left out entirely, so that
    comparable() can tell "both sides say point" from "neither side said".
    symbology=False leaves the drawingInfo and the scale range out unread, so
    a renderer this file refuses cannot stop a --schema-only run, which
    version 1.0 compared without reading them.
    """
    if not isinstance(raw, dict):
        raise ValueError("a layer definition must be an object, got %r"
                         % (type(raw).__name__,))
    fields = raw.get("fields")
    if fields is None:
        fields = []
    if not isinstance(fields, list):
        raise ValueError("the fields of a layer must be a list, got %r"
                         % (type(fields).__name__,))
    out = {"label": label, "name": "%s" % (raw.get("name") or ""),
           "fields": [normalize_field(f) for f in fields]}
    seen = {}
    for field in out["fields"]:
        key = field["name"].upper()
        if key in seen:
            raise ValueError("the layer has two fields named %s, so no "
                             "comparison of it can be trusted" % field["name"])
        seen[key] = True
    if raw.get("geometryType"):
        out["geometryType"] = "%s" % raw["geometryType"]
    key = sr_key(layer_spatial_reference(raw))
    if key is not None:
        out["spatialReference"] = key
    if raw.get("maxRecordCount") is not None:
        count = raw["maxRecordCount"]
        if not isinstance(count, int) or isinstance(count, bool):
            raise ValueError("maxRecordCount must be a number, got %r"
                             % (count,))
        out["maxRecordCount"] = count
    if raw.get("capabilities") is not None:
        out["capabilities"] = capability_set(raw["capabilities"])
    if "types" in raw or "subtypes" in raw or "subtypeField" in raw \
            or "subtypeFieldName" in raw:
        out["subtypes"] = subtype_key(raw)
    if not symbology:
        return out
    if raw.get("drawingInfo") is not None:
        out["drawingInfo"] = drawing_key(raw["drawingInfo"])
    scales = scale_range(raw)
    if scales is not None:
        out["scaleRange"] = scales
    return out


def layer_spatial_reference(raw):
    """Where a layer definition actually keeps its spatial reference.

    A published layer reports it inside extent and not at the top level, so a
    tool that reads only the top level never compares projections at all and
    says nothing on the one change that moves every feature.
    """
    top = raw.get("spatialReference")
    if top is not None:
        return top
    extent = raw.get("extent")
    if isinstance(extent, dict):
        return extent.get("spatialReference")
    return None


def capability_set(raw):
    """The capabilities string as a set, upper cased and order independent."""
    if isinstance(raw, (list, tuple)):
        parts = raw
    elif isinstance(raw, str):
        parts = raw.split(",")
    else:
        raise ValueError("capabilities must be a string or a list, got %r"
                         % (raw,))
    return frozenset(("%s" % p).strip().upper() for p in parts
                     if ("%s" % p).strip())


def comparable(left, right, key):
    """True when both sides report the property, so it can be compared at all.

    An arcpy-read feature class has no maxRecordCount and no capabilities. A
    tool that reads those as absent reports every dataset comparison as having
    lost the capabilities of the service, which is noise on every run.
    """
    return key in left and key in right


def pair_fields(left_fields, right_fields):
    """Match the two field lists up. Returns (pairs, only_left, only_right).

    Names first, case insensitively, because a geodatabase column called owner
    published as OWNER is one field and not two.

    Then the identity rescue, which is the reason this function exists. An
    object id field and a global id field are a ROLE, not a name: the publish
    decides what they are called and the source has no say. Left with name
    matching alone, a source whose object id is FID against a service whose
    object id is OBJECTID reports FID removed and OBJECTID added, on every
    comparison of every layer, and the tool is noise. So one unmatched identity
    field on each side with the same role is a pair. Two unmatched ones on a
    side are left alone: there is nothing to tell them apart with.
    """
    right_index = {}
    for field in right_fields:
        right_index[field["name"].upper()] = field
    pairs = []
    only_left = []
    for field in left_fields:
        match = right_index.pop(field["name"].upper(), None)
        if match is None:
            only_left.append(field)
        else:
            pairs.append((field, match))
    only_right = [f for f in right_fields if f["name"].upper() in right_index]

    for role in ("oid", "globalid"):
        left_role = [f for f in only_left if f["role"] == role]
        right_role = [f for f in only_right if f["role"] == role]
        if len(left_role) == 1 and len(right_role) == 1:
            pairs.append((left_role[0], right_role[0]))
            only_left.remove(left_role[0])
            only_right.remove(right_role[0])
    return pairs, only_left, only_right


def diff_one_field(left, right):
    """Every difference between two fields already known to be the same field."""
    out = []
    renamed = left["name"].upper() != right["name"].upper()
    if renamed:
        out.append(Difference(
            "IDENTITY_RENAMED", WARNING, right["name"],
            "the %s field is %s in the source and %s in the service"
            % (left["role"] or "identity", left["name"], right["name"])))
    if left["type"] != right["type"]:
        if is_widening(left["type"], right["type"]):
            out.append(Difference(
                "TYPE_WIDENED", WARNING, right["name"],
                "%s widened to %s, which holds every value it held"
                % (left["type"], right["type"])))
        else:
            out.append(Difference(
                "TYPE_CHANGED", BREAK, right["name"],
                "%s in the source, %s in the service"
                % (left["type"], right["type"])))
    elif left["length"] is not None and right["length"] is not None \
            and left["length"] != right["length"]:
        # Only when the type did not change. A text field republished as a
        # number has a length on one side and none on the other, and saying so
        # twice tells nobody anything the type change did not already say.
        if right["length"] < left["length"]:
            out.append(Difference(
                "LENGTH_DECREASED", BREAK, right["name"],
                "length %d in the source, %d in the service, so a value that "
                "fits the source is truncated"
                % (left["length"], right["length"])))
        else:
            out.append(Difference(
                "LENGTH_INCREASED", WARNING, right["name"],
                "length %d in the source, %d in the service"
                % (left["length"], right["length"])))
    if not renamed and left["alias"] != right["alias"]:
        # A renamed identity field almost always carries a different alias too,
        # and reporting both says the same thing twice.
        out.append(Difference(
            "ALIAS_CHANGED", WARNING, right["name"],
            "alias %r in the source, %r in the service"
            % (left["alias"], right["alias"])))
    change = domain_change(left["domain"], right["domain"])
    if change == "removed":
        out.append(Difference(
            "DOMAIN_REMOVED", BREAK, right["name"],
            "the source has the domain %s and the service has none, so the "
            "picklist and the validation are gone"
            % (left["domain"][1] or "(unnamed)",)))
    elif change == "added":
        out.append(Difference(
            "DOMAIN_ADDED", WARNING, right["name"],
            "the service has the domain %s and the source has none"
            % (right["domain"][1] or "(unnamed)",)))
    elif change == "changed":
        out.append(Difference(
            "DOMAIN_CHANGED", WARNING, right["name"],
            "domain %s in the source, %s in the service"
            % (describe_domain(left["domain"]),
               describe_domain(right["domain"]))))
    return out


def describe_domain(key):
    """A domain key as a short phrase."""
    if key is None:
        return "none"
    if key[2]:
        return "%s %s with %d code(s)" % (key[1] or "(unnamed)", key[0],
                                          len(key[2]))
    return "%s %s" % (key[1] or "(unnamed)", key[0])


def data_fields(layer):
    """The fields that hold data, so the shape is left out of both comparisons.

    arcpy lists the geometry field of every feature class and a published
    service often does not, so comparing the two lists straight reports the
    shape as a field the service lost, on every dataset comparison ever run.
    What the geometry actually is gets compared as geometryType, once.
    """
    return [f for f in layer["fields"]
            if f["type"] != "esriFieldTypeGeometry"]


def diff_fields(left, right):
    """Field level differences between two normalized layers."""
    pairs, only_left, only_right = pair_fields(data_fields(left),
                                               data_fields(right))
    out = []
    for field in only_left:
        out.append(Difference(
            "FIELD_REMOVED", BREAK, field["name"],
            "%s in the source, absent from the service" % field["type"]))
    for field in only_right:
        out.append(Difference(
            "FIELD_ADDED", WARNING, field["name"],
            "%s in the service, absent from the source" % field["type"]))
    for left_field, right_field in pairs:
        out.extend(diff_one_field(left_field, right_field))
    return out


def diff_layer_properties(left, right):
    """Differences in what the layer is, rather than in what it holds."""
    out = []
    for key, kind, severity in PLAIN_PROPERTIES:
        if not comparable(left, right, key):
            continue
        if left[key] == right[key]:
            continue
        if key == "subtypes" and right[key] is None:
            out.append(Difference(
                "SUBTYPES_REMOVED", BREAK, key,
                "the source has subtypes on %s and the service has none"
                % (left[key][0] or "(no field)",)))
            continue
        out.append(Difference(
            kind, severity, key, "%s in the source, %s in the service"
            % (property_text(left[key]), property_text(right[key]))))
    if comparable(left, right, "capabilities"):
        for name in sorted(left["capabilities"] - right["capabilities"]):
            out.append(Difference(
                "CAPABILITY_REMOVED", BREAK, "capabilities",
                "the source allows %s and the service does not" % name))
        for name in sorted(right["capabilities"] - left["capabilities"]):
            out.append(Difference(
                "CAPABILITY_ADDED", WARNING, "capabilities",
                "the service allows %s and the source does not" % name))
    return out


def property_text(value):
    """A layer property as one readable phrase."""
    if value is None:
        return "none"
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "wkid":
        return "wkid %s" % (value[1],)
    if isinstance(value, tuple) and len(value) == 2 and value[0] == "wkt":
        return "a wkt of %d character(s)" % len(value[1])
    if isinstance(value, tuple) and len(value) == 2:
        return "%s with %d code(s)" % (value[0] or "(no field)", len(value[1]))
    return "%s" % (value,)


def sort_differences(diffs):
    """A stable order: breaks first, then by what they are about.

    Sorted rather than left in discovery order, so that reordering the fields
    of either side cannot change one byte of the report.
    """
    return sorted(diffs, key=lambda d: (SEVERITY_RANK.get(d.severity, 9),
                                        d.target.upper(), d.kind, d.detail))


def diff_layers(left, right, symbology=True):
    """Every difference between two normalized layers, in report order.

    symbology=False compares the fields and the layer properties only, which
    is what version 1.0 compared and what --schema-only asks for.
    """
    out = diff_fields(left, right) + diff_layer_properties(left, right)
    if symbology:
        out.extend(diff_symbology(left, right))
    return sort_differences(out)


# ------------------------------------------------------------ symbology core
#
# A schema check passes a republish that draws the right data the wrong way.
# The renderer is JSON, and a byte compare of it reports every republish as
# drift, because a server reorders keys, drops the alpha off a colour, writes
# 0.7 back as 0.7000000000000001 and spells defaults out. So both sides are
# reduced to one canonical form first, and only then compared, by rules that
# know which difference moves a feature into another class and which only
# changes how the class looks.

def is_number(value):
    """True for an int or a float, and False for a bool, which Python counts."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def numbers_close(a, b):
    """True when two numbers differ by no more than the tolerances above.

    One rule for an integer and a float. An earlier version compared two
    integers exactly and anything else at a tolerance of 1e-6, so a break
    edited from 1000000 to 1000001 was a change, and the same edit read as
    no change when the server had written the break as 1000000.0.
    """
    if a == b:
        return True
    try:
        a, b = float(a), float(b)
    except OverflowError:
        # JSON allows an integer with four hundred digits and a float does
        # not hold one. Two of those that are not equal are not close.
        return False
    if a != a or b != b:
        # json.loads reads NaN, and NaN equals nothing, so without this a
        # layer holding one never equals its own snapshot and fails nightly.
        return a != a and b != b
    # isclose counts an infinity close to itself only.
    return math.isclose(a, b, rel_tol=NUMBER_TOLERANCE,
                        abs_tol=ABSOLUTE_TOLERANCE)


def same_value(left, right):
    """Equal, with numbers compared inside the tolerance and a bool not a 1."""
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if is_number(left) and is_number(right):
        return numbers_close(left, right)
    # A number against a non-number is never equal here: every other JSON
    # value is a str, list, dict or None, and none of those equals a number.
    return left == right


def is_colour(key, value):
    """True for an Esri colour: three or four channels under a *color key.

    canonical_drawing hands each item of a *colors list, a colorInfo's ramp,
    the key "color", so a colour in a list is a colour too.

    A channel is a number that rounds into 0 to 255. Anything else, an
    infinity or a NaN included, is left as it stands: int(round()) of an
    infinity raises OverflowError, which main() does not catch.
    """
    return (key.lower().endswith("color") and isinstance(value, list)
            and len(value) in (3, 4)
            and all(is_number(c) and -0.5 < c < 255.5 for c in value))


def canonical_drawing(value, key=""):
    """A drawingInfo, or any part of one, with the noise of a republish gone.

    Key order is dropped by the comparison, not here. What goes here: the
    order of the stops of a visual variable; a null
    and an empty object or list, which say the same as a key that is absent;
    a key that holds its default; authoringInfo; a colour's missing alpha and
    its float noise, in a colors list as well; the edge whitespace of an
    expression; and the url of a
    picture symbol that carries its own image, because the url is the name the
    server filed the image under and a republish may file it again.
    """
    if isinstance(value, dict):
        out = {}
        for name in value:
            if name in IGNORED_DRAWING_KEYS:
                continue
            item = canonical_drawing(value[name], name)
            if item is None or item == {} or item == []:
                continue
            if name in DRAWING_DEFAULTS \
                    and same_value(item, DRAWING_DEFAULTS[name]):
                continue
            out[name] = item
        if "imageData" in out:
            out.pop("url", None)
        return out
    if isinstance(value, list):
        if is_colour(key, value):
            channels = [int(round(c)) for c in value]
            return channels + [255] * (4 - len(channels))
        # The items of a colors list are colours. Handed a key of "" they
        # kept their float noise and lost no alpha, which is drift in a ramp
        # nobody changed.
        inner = "color" if key.lower().endswith("colors") else ""
        items = [canonical_drawing(item, inner) for item in value]
        if key == "stops" and all(isinstance(stop, dict)
                                  and is_number(stop.get("value"))
                                  and stop["value"] == stop["value"]
                                  for stop in items):
            # A stop is placed on the ramp by its value, not by where it is
            # listed. The sort is stable, so two stops on one value, which
            # make a hard edge, keep the order that gives the edge its sides.
            items.sort(key=lambda stop: stop["value"])
        return items
    if isinstance(value, str) and key in EXPRESSION_KEYS:
        return value.replace("\r\n", "\n").strip()
    return value


def join_path(path, key):
    """A dotted path into a drawingInfo, for the report."""
    return "%s.%s" % (path, key) if path else key


def first_mismatch(left, right, path=""):
    """None when two canonical values agree, else (path, left, right).

    Objects are compared key by key in sorted order, so key order is never a
    difference. Lists are compared position by position.
    """
    if isinstance(left, dict) and isinstance(right, dict):
        for key in sorted(set(left) | set(right)):
            hit = first_mismatch(left.get(key), right.get(key),
                                 join_path(path, key))
            if hit is not None:
                return hit
        return None
    if isinstance(left, list) and isinstance(right, list) \
            and len(left) == len(right):
        for index in range(len(left)):
            hit = first_mismatch(left[index], right[index],
                                 "%s[%d]" % (path, index))
            if hit is not None:
                # A colour is one value. "color[0] is 255" names a channel
                # nobody set on its own, so the whole colour is reported, and
                # so is one colour of a colors list.
                if re.search(r"(?i)(color|colors\[\d+\])$", path):
                    return (path, left, right)
                return hit
        return None
    if same_value(left, right):
        return None
    return (path or "(the whole value)", left, right)


def value_text(value):
    """A drawingInfo value as short JSON, for one line of the report."""
    if value is None:
        # Absent, null, empty or the default: canonical_drawing has made
        # those one thing, and "unset" is true of all four.
        return "unset"
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= 60 else text[:57] + "..."


def mismatch_text(prefix, hit):
    """One mismatch from first_mismatch as a sentence."""
    return "%s%s is %s in the source, %s in the service" % (
        prefix, hit[0], value_text(hit[1]), value_text(hit[2]))


def class_value(value):
    """One spelling for a class value or a number in the report.

    A republish writes a coded value 1 back as "1" or 1.0, and those are one
    class, not one class removed and one added. A fraction is written to 15
    significant digits, which is all a double holds exactly, so 0.3 written
    back as 0.30000000000000004 is still 0.3. NUMBER_TOLERANCE is not used
    here: a class value is a key the server matches exactly, not a measure.
    """
    if is_number(value):
        if isinstance(value, float) and value.is_integer():
            return "%d" % value
        if isinstance(value, float):
            return "%.15g" % value
        return "%d" % value
    return "%s" % (value,)


def as_list(value, what):
    """A list, or [] for nothing, or a ValueError main() turns into exit 2."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("%s must be a list, got %r" % (what, value))
    return value


def as_object(value, what):
    """An object, or a ValueError main() turns into exit 2."""
    if not isinstance(value, dict):
        raise ValueError("%s must be an object, got %r" % (what, value))
    return value


def unique_classes(renderer):
    """The classes of a unique value renderer, keyed by the value they draw.

    uniqueValueInfos is read when it is there. A newer server writes the same
    classes a second time as uniqueValueGroups, which is then ignored; a side
    that has only the groups is read from them instead.
    """
    infos = renderer.get("uniqueValueInfos")
    pairs = []
    if infos is None:
        for group in as_list(renderer.get("uniqueValueGroups"),
                             "uniqueValueGroups"):
            group = as_object(group, "a unique value group")
            for cls in as_list(group.get("classes"), "a group's classes"):
                cls = as_object(cls, "a unique value class")
                for values in as_list(cls.get("values"), "a class's values"):
                    pairs.append((tuple(class_value(v) for v in
                                        as_list(values, "a class value")),
                                  cls))
    else:
        multi = bool(renderer.get("field2") or renderer.get("field3"))
        delimiter = renderer.get("fieldDelimiter", ",")
        if multi and (not isinstance(delimiter, str) or not delimiter):
            # str.split would raise a TypeError on a number, which main()
            # does not catch, and a bare ValueError on "" that says nothing.
            raise ValueError("fieldDelimiter must be text, got %r"
                             % (delimiter,))
        for info in as_list(infos, "uniqueValueInfos"):
            info = as_object(info, "a unique value class")
            value = info.get("value")
            if multi and isinstance(value, str):
                # Pro writes ", " and a server may write ",", so each part is
                # trimmed: "A, R1" and "A,R1" are the same pair of values.
                key = tuple(part.strip() for part in value.split(delimiter))
            else:
                key = (class_value(value),)
            pairs.append((key, info))
    out = {}
    for key, info in pairs:
        if key in out:
            raise ValueError("the renderer has two classes for the value %s, "
                             "so no comparison of it can be trusted"
                             % ",".join(key))
        out[key] = info
    return out


def break_classes(renderer):
    """The classes of a class breaks renderer as ((low, high), class) pairs.

    Sorted by the upper bound, so the order the server listed them in is not
    a difference. A lower bound is the class's own classMinValue when it has
    one, the upper bound of the class below it when it does not, and the
    renderer's minValue for the first class. A server that writes
    classMinValue out on every class therefore reads the same as one that
    does not, and a low of None means the side did not say.
    """
    rows = []
    for info in as_list(renderer.get("classBreakInfos"), "classBreakInfos"):
        info = as_object(info, "a class break")
        top = info.get("classMaxValue")
        if not is_number(top) or top != top:
            # A NaN, which json.loads reads, is not a number either. It has
            # no place in the sort below, so the classes would pair by chance.
            raise ValueError("a class break with no numeric classMaxValue: %r"
                             % (info,))
        rows.append((top, info))
    rows.sort(key=lambda row: row[0])
    lower = renderer.get("minValue")
    if not is_number(lower):
        lower = None
    out = []
    for top, info in rows:
        bottom = info.get("classMinValue")
        if not is_number(bottom):
            bottom = lower
        out.append(((bottom, top), info))
        lower = top
    return out


def same_bounds(left, right):
    """True when two lists of class breaks put every value in the same class."""
    if len(left) != len(right):
        return False
    for (left_bounds, _l), (right_bounds, _r) in zip(left, right):
        if not numbers_close(left_bounds[1], right_bounds[1]):
            return False
        if left_bounds[0] is not None and right_bounds[0] is not None \
                and not numbers_close(left_bounds[0], right_bounds[0]):
            return False
    return True


def breaks_text(classes):
    """The break values of a class breaks renderer, as one phrase.

    When a class's own classMinValue does not meet the top of the class below
    it, every class is named by its range. The short list prints only the
    first lower bound, so a moved interior lower bound read the same on both
    sides of a line that reported it as a change.
    """
    if not classes:
        return "no classes"
    if not all(numbers_close(bounds[0], below[0][1])
               for below, (bounds, _info) in zip(classes, classes[1:])):
        return ", ".join(bounds_name(bounds) for bounds, _info in classes)
    parts = []
    if classes[0][0][0] is not None:
        parts.append(class_value(classes[0][0][0]))
    parts.extend(class_value(bounds[1]) for bounds, _info in classes)
    return ", ".join(parts)


def bounds_name(bounds):
    """One class break's range, as the report names it."""
    if bounds[0] is None:
        return "up to %s" % class_value(bounds[1])
    return "%s to %s" % (class_value(bounds[0]), class_value(bounds[1]))


def renderer_reads(renderer):
    """What a renderer reads its value out of, as {key: value}.

    A dict rather than text, so first_mismatch compares a normalizationTotal
    inside the tolerance, as every other number here is compared.
    """
    out = {}
    for key in RENDERER_FIELD_KEYS:
        value = renderer.get(key)
        if isinstance(value, str) and value.strip():
            # Case folded for the same reason field names are: a column
            # called status published as STATUS is one column.
            out[key] = value.strip().upper()
    for key in RENDERER_RULE_KEYS:
        value = renderer.get(key)
        if value:
            out[key] = value
    return out


def reads_text(reads):
    """What renderer_reads found, as a phrase for the report."""
    return " and ".join("%s %s" % (key, class_value(reads[key]))
                        for key in RENDERER_FIELD_KEYS + RENDERER_RULE_KEYS
                        if key in reads) or "nothing"


def diff_class(name, left, right):
    """One class against the same class: its symbol, then its legend text.

    A class symbol is a BREAK. It is the key a reader decodes the map with,
    and a class drawn in the colour another class used to have says
    something false about every feature in it.
    """
    out = []
    hit = first_mismatch(left.get("symbol"), right.get("symbol"), "symbol")
    if hit is not None:
        out.append(Difference("CLASS_SYMBOL_CHANGED", BREAK, "renderer",
                              mismatch_text("class %s: " % name, hit)))
    rest_left = dict((k, v) for k, v in left.items() if k not in CLASS_KEYS)
    rest_right = dict((k, v) for k, v in right.items() if k not in CLASS_KEYS)
    hit = first_mismatch(rest_left, rest_right)
    if hit is not None:
        out.append(Difference("CLASS_LABEL_CHANGED", WARNING, "renderer",
                              mismatch_text("class %s: " % name, hit)))
    return out


def diff_unique_classes(left, right):
    """Unique value classes matched by value, never by position."""
    left_classes = unique_classes(left)
    right_classes = unique_classes(right)
    out = []
    for key in sorted(set(left_classes) | set(right_classes)):
        name = ",".join(key)
        if key not in right_classes:
            out.append(Difference(
                "CLASS_REMOVED", BREAK, "renderer",
                "the value %s has a class in the source and none in the "
                "service, so those features draw as the default symbol or "
                "not at all" % name))
        elif key not in left_classes:
            out.append(Difference(
                "CLASS_ADDED", WARNING, "renderer",
                "the value %s has a class in the service and none in the "
                "source" % name))
        else:
            out.extend(diff_class(name, left_classes[key], right_classes[key]))
    return out


def diff_break_classes(left, right):
    """Class breaks: the break values first, and the symbols only if they hold.

    When a break moved, a symbol compared class by class is a symbol compared
    against a different range, so the moved break is the one finding.
    """
    left_classes = break_classes(left)
    right_classes = break_classes(right)
    if not same_bounds(left_classes, right_classes):
        return [Difference(
            "CLASS_BREAKS_CHANGED", BREAK, "renderer",
            "breaks %s in the source, %s in the service, so a feature can "
            "land in a different class" % (breaks_text(left_classes),
                                           breaks_text(right_classes)))]
    out = []
    for (bounds, info_left), (_bounds, info_right) in zip(left_classes,
                                                          right_classes):
        out.extend(diff_class(bounds_name(bounds), info_left, info_right))
    return out


def diff_default_symbol(left, right):
    """The symbol for a feature no class claims."""
    symbol_left = left.get("defaultSymbol")
    symbol_right = right.get("defaultSymbol")
    if symbol_left is not None and symbol_right is None:
        return [Difference(
            "DEFAULT_SYMBOL_REMOVED", BREAK, "renderer",
            "the source draws a feature that matches no class and the service "
            "does not draw it at all")]
    if symbol_left is None and symbol_right is not None:
        return [Difference(
            "DEFAULT_SYMBOL_ADDED", WARNING, "renderer",
            "the service draws a feature that matches no class and the source "
            "did not draw it")]
    hit = first_mismatch(symbol_left, symbol_right, "defaultSymbol")
    if hit is None:
        return []
    return [Difference("DEFAULT_SYMBOL_CHANGED", WARNING, "renderer",
                       mismatch_text("", hit))]


def diff_renderer(left, right):
    """Two canonical renderers, either of which may be None.

    What a feature MEANS on the map is a break: the renderer type, the field
    it reads, the class breaks, a class that is gone, the symbol of a class,
    a lost default symbol, the field a visual variable reads, a colour ramp
    and a lost visual variable. How the map looks without meaning anything
    different is a warning: the one symbol of a simple renderer, the default
    symbol's look, a legend label, a size ramp and any other property.
    """
    left = left or {}
    right = right or {}
    type_left = left.get("type") or "none"
    type_right = right.get("type") or "none"
    if type_left != type_right:
        # And nothing else. The classes of two different kinds of renderer
        # are not the same classes, and comparing them is noise.
        return [Difference(
            "RENDERER_TYPE_CHANGED", BREAK, "renderer",
            "%s in the source, %s in the service, so every feature is drawn "
            "by a different rule" % (type_left, type_right))]
    reads_left = renderer_reads(left)
    reads_right = renderer_reads(right)
    if first_mismatch(reads_left, reads_right) is not None:
        # And nothing else, for the same reason: every class now holds other
        # features, so a class that happens to match by value still differs.
        return [Difference(
            "RENDERER_FIELD_CHANGED", BREAK, "renderer",
            "the renderer reads %s in the source and %s in the service"
            % (reads_text(reads_left), reads_text(reads_right)))]
    out = []
    if type_left == "uniqueValue":
        out.extend(diff_unique_classes(left, right))
    elif type_left == "classBreaks":
        out.extend(diff_break_classes(left, right))
    hit = first_mismatch(left.get("symbol"), right.get("symbol"), "symbol")
    if hit is not None:
        # The one symbol of a simple renderer encodes no value, so a new
        # colour on it says nothing false about any feature.
        out.append(Difference("SYMBOL_CHANGED", WARNING, "renderer",
                              mismatch_text("", hit)))
    out.extend(diff_default_symbol(left, right))
    out.extend(diff_visual_variables(left, right))
    out.extend(diff_attributes(left, right))
    for key in sorted((set(left) | set(right)) - RENDERER_HANDLED_KEYS):
        hit = first_mismatch(left.get(key), right.get(key), key)
        if hit is not None:
            out.append(Difference("RENDERER_CHANGED", WARNING, "renderer",
                                  mismatch_text("", hit)))
    return out


def drawn_part(item, keys):
    """The keys of item that say what a value is drawn as, without legend text.

    A stop's label is legend text, so it is left out here and compared with
    the rest of the item, as a warning.
    """
    out = dict((key, item[key]) for key in keys if key in item)
    if isinstance(out.get("stops"), list):
        out["stops"] = [dict((k, v) for k, v in stop.items() if k != "label")
                        if isinstance(stop, dict) else stop
                        for stop in out["stops"]]
    return out


def diff_encoding(prefix, left, right, drawn_keys, kinds):
    """One visual variable or one attribute against its pair.

    kinds is (read, drawn, rest). What the item reads, its field, expression
    or normalization, is a BREAK, as the renderer's own field is. drawn_keys
    say what a value is drawn as, and a change there is a BREAK, as a class
    symbol is. Both are reported, so a ramp that moved to another field and
    another colour says so twice. Anything else is a warning, and is compared
    only when neither of those changed.
    """
    out = []
    reads_left = renderer_reads(left)
    reads_right = renderer_reads(right)
    if first_mismatch(reads_left, reads_right) is not None:
        out.append(Difference(
            kinds[0], BREAK, "renderer",
            "%sit reads %s in the source and %s in the service"
            % (prefix, reads_text(reads_left), reads_text(reads_right))))
    hit = first_mismatch(drawn_part(left, drawn_keys),
                         drawn_part(right, drawn_keys))
    if hit is not None:
        out.append(Difference(kinds[1], BREAK, "renderer",
                              mismatch_text(prefix, hit)))
    if not out:
        hit = first_mismatch(left, right)
        if hit is not None:
            out.append(Difference(kinds[2], WARNING, "renderer",
                                  mismatch_text(prefix, hit)))
    return out


def diff_visual_variables(left, right):
    """Visual variables paired by type and target, then in canonical order.

    A draft of this version called every visual variable difference a
    warning. On a map coloured by a colorInfo the visual variable is the
    classification, so a colorInfo moved from POP to INCOME recoloured every
    feature by another attribute and exited 0.
    """
    groups_left = visual_variables(left)
    groups_right = visual_variables(right)
    out = []
    for key in sorted(set(groups_left) | set(groups_right)):
        name = variable_name(key)
        items_left = groups_left.get(key, [])
        items_right = groups_right.get(key, [])
        drawn_keys = COLOUR_RAMP_KEYS if key[0] == "colorInfo" else ()
        for one_left, one_right in zip(items_left, items_right):
            out.extend(diff_encoding(
                "visual variable %s: " % name, one_left, one_right,
                drawn_keys, ("VARIABLE_FIELD_CHANGED",
                             "COLOUR_RAMP_CHANGED",
                             "VISUAL_VARIABLES_CHANGED")))
        for _extra in items_left[len(items_right):]:
            out.append(Difference(
                "VISUAL_VARIABLE_REMOVED", BREAK, "renderer",
                "the source has the visual variable %s and the service does "
                "not, so the value it showed is gone from the map" % name))
        for _extra in items_right[len(items_left):]:
            out.append(Difference(
                "VISUAL_VARIABLE_ADDED", WARNING, "renderer",
                "the service has the visual variable %s and the source does "
                "not" % name))
    return out


def renderer_attributes(renderer):
    """The attributes of a dot density or pie chart renderer, checked."""
    return [as_object(item, "an attribute")
            for item in as_list(renderer.get("attributes"), "attributes")]


def diff_attributes(left, right):
    """Dot density and pie chart attributes, paired by position.

    Each attribute is a class: the field it counts and the colour it is drawn
    in. Position is the pairing because a pie chart draws its slices in that
    order. Compared as plain JSON, a dot density map moved from POP to
    HOUSING was a warning that exited 0.
    """
    items_left = renderer_attributes(left)
    items_right = renderer_attributes(right)
    out = []
    for index, pair in enumerate(zip(items_left, items_right)):
        out.extend(diff_encoding(
            "attribute %d: " % index, pair[0], pair[1], ("color",),
            ("RENDERER_FIELD_CHANGED", "CLASS_SYMBOL_CHANGED",
             "CLASS_LABEL_CHANGED")))
    for index in range(len(items_right), len(items_left)):
        out.append(Difference(
            "CLASS_REMOVED", BREAK, "renderer",
            "attribute %d is in the source and not in the service, so what it "
            "counted is gone from the map" % index))
    for index in range(len(items_left), len(items_right)):
        out.append(Difference(
            "CLASS_ADDED", WARNING, "renderer",
            "attribute %d is in the service and not in the source" % index))
    return out


def grouped(items, what, one, key_of):
    """A list whose order means nothing and whose items have no id, grouped.

    Label classes and visual variables are both lists like that. The items
    are grouped by key_of, and each group is put in the order of its
    canonical JSON, so the order they were listed in is not a difference.
    """
    groups = {}
    for item in as_list(items, what):
        item = as_object(item, one)
        groups.setdefault(key_of(item), []).append(item)
    for key in groups:
        groups[key].sort(key=lambda c: json.dumps(c, sort_keys=True))
    return groups


def label_key(cls):
    """What a label class labels: (expression, where)."""
    info = cls.get("labelExpressionInfo")
    expression = info.get("expression") if isinstance(info, dict) else None
    if not expression:
        expression = cls.get("labelExpression") or ""
    return ("%s" % expression, "%s" % (cls.get("where") or ""))


def label_classes(labeling):
    """Label classes grouped by the expression that makes the text and the
    where clause that picks the features. A label class has no id."""
    return grouped(labeling, "labelingInfo", "a label class", label_key)


def visual_variables(renderer):
    """Visual variables grouped by (type, target).

    A colorInfo and a sizeInfo each drive a property of their own, so the
    order they are listed in draws nothing differently. target tells a size
    on the outline from a size on the symbol.
    """
    return grouped(renderer.get("visualVariables"), "visualVariables",
                   "a visual variable",
                   lambda item: ("%s" % (item.get("type") or ""),
                                 "%s" % (item.get("target") or "")))


def label_name(key):
    """A label class, as the report names it."""
    name = key[0] or "(no expression)"
    return "%s where %s" % (name, key[1]) if key[1] else name


def variable_name(key):
    """A visual variable, as the report names it."""
    name = key[0] or "(no type)"
    return "%s on %s" % (name, key[1]) if key[1] else name


def diff_groups(left_groups, right_groups, name_of, target, kinds, texts):
    """Two grouped lists, paired by key and then in canonical order.

    kinds is (changed, removed, added). texts is the prefix of a change and
    the sentences for a removal and an addition, each with %s for the name.
    Every difference here is a warning. Only label classes come here, and no
    feature moves into another class because of a label.
    """
    out = []
    for key in sorted(set(left_groups) | set(right_groups)):
        name = name_of(key)
        items_left = left_groups.get(key, [])
        items_right = right_groups.get(key, [])
        for one_left, one_right in zip(items_left, items_right):
            hit = first_mismatch(one_left, one_right)
            if hit is not None:
                out.append(Difference(kinds[0], WARNING, target,
                                      mismatch_text(texts[0] % name, hit)))
        for _extra in items_left[len(items_right):]:
            out.append(Difference(kinds[1], WARNING, target, texts[1] % name))
        for _extra in items_right[len(items_left):]:
            out.append(Difference(kinds[2], WARNING, target, texts[2] % name))
    return out


def diff_labels(left, right):
    """Label classes. Every label difference is a warning: no feature moves."""
    return diff_groups(
        label_classes(left), label_classes(right), label_name, "labelingInfo",
        ("LABEL_CLASS_CHANGED", "LABEL_CLASS_REMOVED", "LABEL_CLASS_ADDED"),
        ("labels %s: ", "the source labels %s and the service does not",
         "the service labels %s and the source does not"))


def drawing_key(raw):
    """A drawingInfo in canonical form, checked before it is compared.

    Checked here rather than when it is compared, so a renderer that cannot
    be trusted is refused at the same point as a field with no name.
    """
    if not isinstance(raw, dict):
        raise ValueError("drawingInfo must be an object, got %r"
                         % (type(raw).__name__,))
    out = canonical_drawing(raw)
    renderer = out.get("renderer")
    if renderer is not None:
        as_object(renderer, "a renderer")
        unique_classes(renderer)
        break_classes(renderer)
        visual_variables(renderer)
        renderer_attributes(renderer)
    label_classes(out.get("labelingInfo"))
    return out


def diff_drawing(left, right):
    """Two canonical drawingInfo objects."""
    out = diff_renderer(left.get("renderer"), right.get("renderer"))
    out.extend(diff_labels(left.get("labelingInfo"), right.get("labelingInfo")))
    for key in sorted((set(left) | set(right))
                      - set(["renderer", "labelingInfo"])):
        hit = first_mismatch(left.get(key), right.get(key), key)
        if hit is not None:
            out.append(Difference("DRAWING_INFO_CHANGED", WARNING,
                                  "drawingInfo", mismatch_text("", hit)))
    return out


def scale_range(raw):
    """(inner, outer) scale denominators the layer draws between, or None.

    maxScale is the inner limit and minScale the outer one, which is the
    wrong way round to read but is how the REST API names them. 0 means no
    limit, so the outer 0 becomes None, which reads as "all the way out".
    """
    if raw.get("minScale") is None and raw.get("maxScale") is None:
        return None
    values = []
    for key in ("maxScale", "minScale"):
        value = raw.get(key)
        if value is None:
            value = 0
        if not is_number(value) or not value >= 0:
            # Written as "not >= 0" so a NaN, which compares false with
            # everything, is refused with the negative numbers.
            raise ValueError("%s must be a scale of zero or more, got %r"
                             % (key, value))
        values.append(value)
    return (values[0], values[1] or None)


def scales_same(left, right):
    """One scale limit against another, where None means no limit."""
    if left is None or right is None:
        return left is None and right is None
    return numbers_close(left, right)


def describe_scales(scales):
    """A visibility range as a phrase."""
    inner, outer = scales
    if not inner and outer is None:
        return "at every scale"
    near = "1:%s" % class_value(inner) if inner else "the closest zoom"
    far = "1:%s" % class_value(outer) if outer is not None else \
        "the widest zoom"
    return "from %s out to %s" % (near, far)


def diff_scale_range(left, right):
    """A visibility range that narrowed is a BREAK, one that widened is not.

    Narrowed means the layer stops drawing at a scale where it drew, which on
    a public map reads as the data being gone.
    """
    same_inner = scales_same(left[0], right[0])
    same_outer = scales_same(left[1], right[1])
    if same_inner and same_outer:
        return []
    narrowed = ((not same_inner and right[0] > left[0])
                or (not same_outer and right[1] is not None
                    and (left[1] is None or right[1] < left[1])))
    detail = "visible %s in the source, %s in the service" % (
        describe_scales(left), describe_scales(right))
    if narrowed:
        return [Difference("VISIBILITY_NARROWED", BREAK, "scaleRange",
                           detail + ", so it stops drawing where it drew")]
    return [Difference("VISIBILITY_WIDENED", WARNING, "scaleRange", detail)]


def diff_symbology(left, right):
    """The renderer, the labels and the visibility range, when both sides say.

    A feature class read through arcpy has none of them, and a side that does
    not report a property is never a side that lost it.
    """
    out = []
    if comparable(left, right, "drawingInfo"):
        out.extend(diff_drawing(left["drawingInfo"], right["drawingInfo"]))
    if comparable(left, right, "scaleRange"):
        out.extend(diff_scale_range(left["scaleRange"], right["scaleRange"]))
    return out


def probe_fields(layer, features):
    """The advertised schema against what a query actually handed back.

    This is the other half of the drift, and the half a schema comparison
    cannot see: the layer definition still lists a field, the data stopped
    carrying it, and every client believes the definition.
    """
    out = []
    if not features:
        return out
    returned = {}
    for feature in features:
        # Checked, as a layer definition is: a feature of null or a list of
        # attributes used to end in an AttributeError, which exits 1.
        feature = as_object(feature, "a feature the query returned")
        for name in as_object(feature.get("attributes") or {},
                              "a feature's attributes"):
            returned[name.upper()] = name
    for field in data_fields(layer):
        # The shape is never an attribute, and data_fields has already left it
        # out, so a layer that lists it is not reported as having lost it.
        if field["name"].upper() not in returned:
            out.append(Difference(
                "FIELD_NOT_RETURNED", BREAK, field["name"],
                "the layer advertises %s and the query did not return it"
                % field["type"]))
    advertised = set(f["name"].upper() for f in data_fields(layer))
    for key in sorted(returned):
        if key not in advertised:
            out.append(Difference(
                "FIELD_NOT_ADVERTISED", WARNING, returned[key],
                "the query returned it and the layer definition does not "
                "list it"))
    return sort_differences(out)


def verdict(diffs):
    """MATCH, WARN or BREAK for a set of differences."""
    for diff in diffs:
        if diff.severity == BREAK:
            return "BREAK"
    return "WARN" if diffs else "MATCH"


def counts(diffs):
    """(breaks, warnings)."""
    breaks = sum(1 for d in diffs if d.severity == BREAK)
    return breaks, len(diffs) - breaks


def exit_code(diffs, strict=False):
    """0 when nothing that matters changed, 1 when something did."""
    breaks, warnings = counts(diffs)
    if breaks:
        return 1
    if strict and warnings:
        return 1
    return 0


def describe(diffs, source_label, service_label, source, service,
             strict=False, probed=None):
    """The report, as lines."""
    lines = ["svcdrift: source -> service"]
    if source is None:
        lines.append("  source:  (none, the service was read on its own)")
    else:
        lines.append("  source:  %s  (%d field(s))"
                     % (source_label, len(source["fields"])))
    lines.append("  service: %s  (%d field(s))"
                 % (service_label, len(service["fields"])))
    lines.append("-" * 68)
    if not diffs:
        lines.append("no differences")
    for diff in diffs:
        lines.append("%-8s %-24s %-18s %s"
                     % (diff.severity, diff.kind, diff.target, diff.detail))
    lines.append("-" * 68)
    if probed is not None:
        lines.append("probe: %d feature(s) read from the service" % probed)
    breaks, warnings = counts(diffs)
    lines.append("%d break(s), %d warning(s)%s"
                 % (breaks, warnings,
                    ", and --strict makes a warning fail" if strict else ""))
    lines.append("VERDICT: %s" % verdict(diffs))
    return lines


def build_report(diffs, source_label, service_label, source, service,
                 strict=False, probed=None):
    """The same report as a document, for --json."""
    breaks, warnings = counts(diffs)
    return {
        "svcdrift": VERSION,
        "source": None if source is None else source_label,
        "service": service_label,
        "sourceFieldCount": None if source is None else len(source["fields"]),
        "serviceFieldCount": len(service["fields"]),
        "probedFeatures": probed,
        "strict": bool(strict),
        "breaks": breaks,
        "warnings": warnings,
        "verdict": verdict(diffs),
        "exitCode": exit_code(diffs, strict),
        "differences": [dict(d._asdict()) for d in diffs],
    }


# ------------------------------------------------------------------ io layer

def redact(text, *secrets):
    """Text with any token taken out of it.

    Two ways a token escapes: the caller's own secret appearing verbatim, and a
    url in an error message that carries token= in its query string. urllib
    quotes the url it could not open back into its own exception, so the second
    one happens without anybody writing a print statement. A secret is also
    taken out in the url-encoded form the request sent it in, which an error
    envelope may echo back. A token= value stops at a backslash, so a JSON
    body redacted before it is parsed keeps its escaped quotes.
    """
    out = "%s" % (text,)
    for secret in secrets:
        if secret:
            for form in (secret, urllib.parse.quote_plus(secret)):
                out = out.replace(form, REDACTED)
    return re.sub(r"(?i)(token=)[^&\s'\"\\]+", r"\1" + REDACTED, out)


def is_http_url(value):
    """True for something urllib will open over http."""
    if not isinstance(value, str):
        return False
    return value.strip().lower().startswith(("http://", "https://"))


def has_userinfo(value):
    """True for an http url with a user name or password before its host.

    urllib cannot log in with one, so the read fails, and the error message
    and the report both quote the url, password and all.
    """
    return bool(re.match(r"(?i)\s*https?://[^/?#]*@", value or ""))


def source_kind(spec):
    """Which of the three sides a --source string names.

    A .json path is a snapshot, an http url is another service, anything else
    is a dataset for arcpy to open.
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("--source needs a value")
    spec = spec.strip()
    if is_http_url(spec):
        return "service"
    if spec.lower().endswith(".json"):
        return "snapshot"
    return "dataset"


def clean_url(base):
    """A layer url with any query string or fragment the caller pasted gone.

    People copy a layer url out of the REST browser and it arrives with ?f=html
    on the end. Keeping that returns a web page to json.loads. It has to be
    stripped HERE, in one place, rather than in the url building alone: an
    operation appended to the raw url lands inside the query string, so
    ".../0?f=html" + "/query" asks for the layer definition and the probe dies
    on a features list that was never going to be there.
    """
    if not is_http_url(base):
        raise ValueError("not an http url: %r" % (base,))
    return base.strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")


def same_host(left, right):
    """True when two urls name the same scheme, host and port.

    The --service token goes only to its own host. Sent to a --source on
    another host, it reaches a server it was not issued for and that server's
    access log, over plain http when the url is http.
    """
    if not is_http_url(left) or not is_http_url(right):
        return False
    try:
        one = urllib.parse.urlsplit(left.strip())
        two = urllib.parse.urlsplit(right.strip())
    except ValueError:
        # An unclosed [ in an ipv6 host. main() calls this before its try,
        # so the ValueError was a traceback that exited 1. The read that
        # follows refuses the url with exit 2 instead.
        return False
    return (one.scheme.lower(), one.netloc.lower()) \
        == (two.scheme.lower(), two.netloc.lower())


def build_url(base, params):
    """A url with f=json on it, and any query string the caller pasted gone."""
    query = dict(params or {})
    query["f"] = "json"
    return "%s?%s" % (clean_url(base),
                      urllib.parse.urlencode(sorted(query.items())))


def error_text(payload):
    """The message out of an ArcGIS error envelope, or "" when there is none.

    ArcGIS Server answers a failure with HTTP 200 and an error object in the
    body. A client that checks the status code reads that as success.
    """
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if not isinstance(error, dict):
        return ""
    parts = ["%s" % (error.get("message") or "the service reported an error")]
    code = error.get("code")
    if code is not None:
        parts.append("(code %s)" % (code,))
    details = error.get("details")
    if not isinstance(details, list):
        # Details that are not a list are printed as they stand. A number
        # there used to be iterated and raise a TypeError, which exits 1.
        details = [] if details is None or details == "" else [details]
    for line in details:
        if "%s" % line != parts[0]:
            parts.append("%s" % line)
    return " ".join(parts)


def service_root_hint(payload):
    """A remedy when the url named a service rather than one of its layers."""
    if not isinstance(payload, dict):
        return ""
    if payload.get("fields") is not None:
        return ""
    layers = payload.get("layers")
    if not isinstance(layers, list) or not layers:
        return ""
    first = 0
    for entry in layers:
        if isinstance(entry, dict) and entry.get("id") is not None:
            first = entry["id"]
            break
    return ("that url is the service and not one of its layers. Add a layer "
            "id, for example /%s" % (first,))


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only to the scheme, host and port it came from.

    urllib follows a Location to any host, and to ftp, with the query string
    still on it. The token then reaches a server it was not issued for, and
    the layer that answers there is compared as if the named one had.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not same_host(req.full_url, newurl):
            raise urllib.error.HTTPError(
                req.full_url, code,
                "a redirect to another host, which was not followed",
                headers, fp)
        return urllib.request.HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


def fetch_json(url, params, token=None, timeout=HTTP_TIMEOUT):
    """GET a url and return the parsed body, or raise RuntimeError.

    Every failure comes back as RuntimeError with a message the token has been
    taken out of, including the failures that arrive as HTTP 200. The body is
    redacted too, before it is parsed and again after, so a token a server
    echoes back in a symbol url or an alias reaches no report and no baseline,
    however the server escaped it.
    """
    query = dict(params or {})
    if token:
        query["token"] = token
    full = build_url(url, query)
    opener = urllib.request.build_opener(_SameHostRedirect)
    try:
        with opener.open(full, timeout=timeout) as response:
            body = redact(response.read().decode("utf-8", "replace"), token)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(redact("%s answered HTTP %s: %s"
                                  % (url, exc.code, exc.reason), token))
    except urllib.error.URLError as exc:
        raise RuntimeError(redact("%s could not be reached: %s"
                                  % (url, exc.reason), token))
    except (OSError, http.client.HTTPException) as exc:
        # http.client raises its own exceptions, not OSError, for a url with
        # a space in it, a port that is not a number, a body cut short and a
        # reply that is not http. Uncaught, each was a traceback that quoted
        # the token and exited 1, the code for a break.
        raise RuntimeError(redact("%s could not be read: %s" % (url, exc),
                                  token))
    try:
        payload = json.loads(body)
    except ValueError:
        raise RuntimeError(redact(
            "%s did not return json. The first 120 characters were: %s"
            % (url, " ".join(body[:120].split())), token))
    message = error_text(payload)
    if message:
        raise RuntimeError(redact("%s: %s" % (url, message), token))
    # The text was redacted, and a JSON escape hides a token from that: a
    # server that writes "/" as "\/", or one letter as a \u escape. Parsed,
    # the escape is gone and the token is plain in what --out writes, so the
    # parsed document is redacted too. With ensure_ascii off, json.dumps
    # escapes only a quote, a backslash and a control character, and a token
    # holds none of those.
    return json.loads(redact(json.dumps(payload, ensure_ascii=False), token))


def read_service(url, token=None, timeout=HTTP_TIMEOUT):
    """The layer definition of a published layer."""
    payload = fetch_json(url, {}, token, timeout)
    hint = service_root_hint(payload)
    if hint:
        raise RuntimeError("%s: %s" % (url, hint))
    return payload


def query_one(url, token=None, timeout=HTTP_TIMEOUT, count=1):
    """Ask the layer for a row, to see which fields the data really carries."""
    payload = fetch_json(clean_url(url) + "/query",
                         {"where": "1=1", "outFields": "*",
                          "returnGeometry": "false",
                          "resultRecordCount": "%d" % count},
                         token, timeout)
    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        raise RuntimeError("%s/query returned no features list" % url)
    return features


def read_snapshot(path):
    """A layer definition out of a file, saved by this tool or by hand."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except ValueError as exc:
        raise RuntimeError("%s is not valid json: %s" % (path, exc))
    except OSError as exc:
        raise RuntimeError("%s could not be read: %s" % (path, exc))
    if isinstance(payload, dict) and isinstance(payload.get("layer"), dict):
        return payload["layer"]
    return payload


def snapshot_document(layer, url, when=None):
    """What --out writes. The url is stored with any token taken off it."""
    stamp = when or datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"svcdrift": VERSION, "read": stamp,
            "url": redact(("%s" % url).split("?", 1)[0]), "layer": layer}


def write_snapshot(document, path):
    """Write the snapshot and return the path, or raise RuntimeError.

    An --out that names a directory or a file with no write access used to
    raise an OSError past main(), which exits 1, the code for a break.
    """
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError as exc:
        raise RuntimeError("%s could not be written: %s" % (path, exc))
    return path


def layer_from_arcpy(describe_object, fields):
    """An arcpy Describe object and its fields, shaped like a layer definition.

    Duck typed on purpose. It reads the attributes arcpy exposes and nothing
    else, so the shaping is testable with stand-ins and the arcpy import stays
    inside read_dataset, where --self-test never reaches it.
    """
    out = {"name": "%s" % (getattr(describe_object, "baseName", "") or ""),
           "fields": []}
    for field in fields:
        item = {"name": field.name, "type": field.type,
                "alias": getattr(field, "aliasName", "") or field.name}
        length = getattr(field, "length", None)
        if isinstance(length, int) and not isinstance(length, bool):
            item["length"] = length
        domain = getattr(field, "domain", "")
        if domain:
            item["domain"] = domain
        out["fields"].append(item)
    shape = "%s" % (getattr(describe_object, "shapeType", "") or "")
    if shape:
        out["geometryType"] = ARCPY_GEOMETRY.get(shape.upper(), shape)
    reference = getattr(describe_object, "spatialReference", None)
    code = getattr(reference, "factoryCode", 0)
    if isinstance(code, int) and not isinstance(code, bool) and code:
        out["spatialReference"] = {"wkid": code}
    return out


def read_dataset(path):
    """A feature class or table, read through arcpy.

    arcpy is imported here and nowhere else. The whole tool works without it:
    a service against a service, or a service against a snapshot, needs no
    geodatabase on the machine at all.
    """
    try:
        import arcpy
    except ImportError:
        raise RuntimeError(
            "%s is not an http url and does not end in .json, so it was read "
            "as a dataset, and arcpy could not be imported. Run this on "
            "ArcGIS Pro's Python, or give --source a layer url or a .json "
            "snapshot." % (path,))
    if not arcpy.Exists(path):
        raise RuntimeError("%s does not exist, or arcpy cannot open it"
                           % (path,))
    return layer_from_arcpy(arcpy.Describe(path), arcpy.ListFields(path))


def read_source(spec, token=None, timeout=HTTP_TIMEOUT):
    """Whichever of the three kinds of source --source named."""
    kind = source_kind(spec)
    if kind == "service":
        return read_service(spec, token, timeout), kind
    if kind == "snapshot":
        return read_snapshot(spec), kind
    return read_dataset(spec), kind


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the whole tool. No arcpy, no portal, no credentials.

    The only socket it opens is a stub http server bound to 127.0.0.1 on a port
    the operating system picks, which the io layer is then driven against.
    """
    import http.server
    import runpy
    import shutil
    import socket
    import tempfile
    import threading
    import time
    import types

    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def fails(fn, label):
        """Something in the io layer that must raise. Returns the message."""
        try:
            fn()
        except RuntimeError as exc:
            check(True, label)
            return "%s" % exc
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)
        return ""

    def refuses(argv, label):
        """argparse writes usage to stderr, swallowed so a green run is green."""
        noise, sys.stderr = sys.stderr, io.StringIO()
        try:
            _parse(argv)
        except SystemExit:
            check(True, label)
        else:
            check(False, "%s (argparse accepted it)" % label)
        finally:
            sys.stderr = noise

    def captured(fn):
        """Run fn with stdout collected. Returns (result, text)."""
        noise, sys.stdout = sys.stdout, io.StringIO()
        try:
            result = fn()
            return result, sys.stdout.getvalue()
        finally:
            sys.stdout = noise

    def one(diffs, kind, target):
        """The single difference of this kind about this target, or None."""
        hits = [d for d in diffs
                if d.kind == kind and d.target.upper() == target.upper()]
        return hits[0] if len(hits) == 1 else None

    def kinds(diffs):
        return sorted(d.kind for d in diffs)

    print("svcdrift self-test: no arcpy, no portal, a stub server on 127.0.0.1")
    print("-" * 68)

    # ---- one spelling for a field type
    check(canonical_type("esriFieldTypeInteger") == "esriFieldTypeInteger",
          "a rest type is already canonical")
    check(canonical_type("Integer") == "esriFieldTypeInteger",
          "arcpy's Integer is the same type as esriFieldTypeInteger")
    check(canonical_type("Long") == "esriFieldTypeInteger",
          "so is arcpy's Long, which is what a geodatabase actually calls it")
    check(canonical_type("SmallInteger") == "esriFieldTypeSmallInteger",
          "arcpy's SmallInteger folds to the rest spelling")
    check(canonical_type("Short") == "esriFieldTypeSmallInteger",
          "and so does Short")
    check(canonical_type("Text") == "esriFieldTypeString",
          "arcpy's Text is a string")
    check(canonical_type("String") == "esriFieldTypeString",
          "and so is String")
    check(canonical_type("Double") == "esriFieldTypeDouble",
          "a double folds")
    check(canonical_type("Float") == "esriFieldTypeSingle",
          "arcpy's Float is a single, not a double  <-- pinned defect")
    check(canonical_type("Date") == "esriFieldTypeDate", "a date folds")
    check(canonical_type("OID") == "esriFieldTypeOID",
          "arcpy's OID is esriFieldTypeOID  <-- pinned defect")
    check(canonical_type("GlobalID") == "esriFieldTypeGlobalID",
          "arcpy's GlobalID is esriFieldTypeGlobalID  <-- pinned defect")
    check(canonical_type("Guid") == "esriFieldTypeGUID",
          "arcpy's Guid is esriFieldTypeGUID, which is a different type again")
    check(canonical_type("Geometry") == "esriFieldTypeGeometry",
          "a geometry field folds")
    check(canonical_type("  Double  ") == "esriFieldTypeDouble",
          "a type with spaces around it folds")
    check(canonical_type("esrifieldtypedouble") == "esriFieldTypeDouble",
          "a rest type in the wrong case is repaired rather than treated as "
          "an unknown type")
    check(canonical_type("esriFieldTypeSomethingNew")
          == "esriFieldTypeSomethingNew",
          "a type this file has never heard of is passed through, so a newer "
          "server does not read as a schema change  <-- pinned defect")
    check(canonical_type("esrifieldtypesomethingnew")
          != canonical_type("esriFieldTypeSomethingNew"),
          "although an unknown type is NOT case folded, because this file has "
          "no way to know what its real spelling is  <-- pinned defect")
    raises(lambda: canonical_type(None), "a field with no type at all raises")
    raises(lambda: canonical_type(""), "an empty type raises")
    raises(lambda: canonical_type("   "), "a whitespace type raises")
    raises(lambda: canonical_type(7), "a numeric type raises")

    # ---- the identity roles
    check(field_role("esriFieldTypeOID") == "oid",
          "an object id field carries the oid role")
    check(field_role("esriFieldTypeGlobalID") == "globalid",
          "a global id field carries the globalid role")
    check(field_role("esriFieldTypeGUID") == "",
          "a plain guid field carries no role: it is data  <-- pinned defect")
    check(field_role("esriFieldTypeString") == "", "a text field carries none")

    # ---- which type changes lose nothing
    check(is_widening("esriFieldTypeSmallInteger", "esriFieldTypeInteger")
          is True,
          "a short integer republished as a long is a widening")
    check(is_widening("esriFieldTypeInteger", "esriFieldTypeBigInteger")
          is True, "a long to a big integer is a widening")
    check(is_widening("esriFieldTypeSmallInteger", "esriFieldTypeBigInteger")
          is True, "and so is a short straight to a big integer")
    check(is_widening("esriFieldTypeSingle", "esriFieldTypeDouble") is True,
          "a single to a double is a widening")
    check(is_widening("esriFieldTypeInteger", "esriFieldTypeSmallInteger")
          is False, "a long back to a short is not: values do not fit")
    check(is_widening("esriFieldTypeDouble", "esriFieldTypeSingle") is False,
          "a double back to a single is not")
    check(is_widening("esriFieldTypeInteger", "esriFieldTypeDouble") is False,
          "an integer to a double is NOT counted as a widening, because a "
          "client that parses integers has to be changed  <-- pinned defect")
    check(is_widening("esriFieldTypeString", "esriFieldTypeInteger") is False,
          "a string to an integer is not a widening")
    check(is_widening("esriFieldTypeInteger", "esriFieldTypeString") is False,
          "and neither is an integer to a string, although every integer "
          "fits in text  <-- pinned defect")
    check(is_widening("esriFieldTypeInteger", "esriFieldTypeInteger") is False,
          "a type that did not change is not a widening")
    check(is_widening("esriFieldTypeOID", "esriFieldTypeInteger") is False,
          "an object id to a plain integer is not a widening")

    # ---- domains
    coded = {"type": "codedValue", "name": "StatusDomain",
             "codedValues": [{"code": "A", "name": "Active"},
                             {"code": "P", "name": "Pending"}]}
    coded_reordered = {"type": "codedValue", "name": "StatusDomain",
                       "codedValues": [{"code": "P", "name": "Pending"},
                                       {"code": "A", "name": "Active"}]}
    coded_extra = {"type": "codedValue", "name": "StatusDomain",
                   "codedValues": [{"code": "A", "name": "Active"},
                                   {"code": "P", "name": "Pending"},
                                   {"code": "X", "name": "Expired"}]}
    ranged = {"type": "range", "name": "AcreRange", "range": [0, 500]}
    check(domain_key(None) is None, "no domain reads as no domain")
    check(domain_key("") is None, "an empty domain name reads as no domain")
    check(domain_key({}) is None, "an empty domain object reads as no domain")
    check(domain_key(coded)[0] == "codedValue", "a coded value domain is read")
    check(domain_key(coded)[1] == "StatusDomain", "with its name")
    check(len(domain_key(coded)[2]) == 2, "and both of its codes")
    check(domain_key(coded) == domain_key(coded_reordered),
          "the same coded values in a different order are the same domain  "
          "<-- pinned defect")
    check(domain_key(coded) != domain_key(coded_extra),
          "one more code is a different domain")
    check(domain_key(ranged)[0] == "range", "a range domain is read")
    check(domain_key(ranged)[2] == (0, 500), "with its bounds")
    check(domain_key("StatusDomain") == ("name", "StatusDomain", ()),
          "a bare domain name is all arcpy gives, and it is read as a name")
    check(domain_key({"type": "inherited", "name": ""})
          == ("inherited", "", ()),
          "a subtype field's inherited domain is read as itself, rather than "
          "as a coded value list it does not have  <-- pinned defect")
    check(domain_key({"name": "Mystery"}) == ("unknown", "Mystery", ()),
          "and a domain with a name and no type at all is read as unknown")
    raises(lambda: domain_key(7), "a numeric domain raises")
    raises(lambda: domain_key({"type": "codedValue",
                               "codedValues": ["Active"]}),
           "a coded value that is a bare string raises a ValueError main() "
           "can turn into an exit code, not an AttributeError traceback  "
           "<-- pinned defect")
    raises(lambda: domain_key({"type": "codedValue", "codedValues": "A"}),
           "and so does a codedValues that is not a list, which was a "
           "TypeError traceback that exited 1, the break code  "
           "<-- pinned defect")
    raises(lambda: domain_key({"type": "range", "range": 5}),
           "and a range domain whose range is not a list  <-- pinned defect")
    check(domain_key({"type": "codedValue", "codedValues": []})
          == ("codedValue", "", ()),
          "and a coded value list with nothing in it is still a coded value "
          "domain")
    check(domain_change(None, None) is None, "no domain either side is no change")
    check(domain_change(None, domain_key(coded)) == "added",
          "a domain the service has and the source does not is an addition")
    check(domain_change(domain_key(coded), None) == "removed",
          "a domain the source has and the service does not is a removal")
    check(domain_change(domain_key(coded), domain_key(coded)) is None,
          "the same domain is no change")
    check(domain_change(domain_key(coded), domain_key(coded_extra))
          == "changed", "a domain that gained a code is a change")
    check(domain_change(domain_key("StatusDomain"), domain_key(coded)) is None,
          "an arcpy domain NAME against the service's full coded value list of "
          "the same name is not a change, because only the name is knowable "
          "on both sides  <-- pinned defect")
    check(domain_change(domain_key(coded), domain_key("StatusDomain")) is None,
          "and the same holds with the sides swapped")
    check(domain_change(domain_key("OtherDomain"), domain_key(coded))
          == "changed", "a different domain name is still a change")
    check(domain_change(domain_key(ranged), domain_key(coded)) == "changed",
          "a range domain against a coded value domain is a change")
    check(describe_domain(None) == "none", "no domain describes as none")
    check("2 code(s)" in describe_domain(domain_key(coded)),
          "a coded value domain describes with its code count")
    check("AcreRange" in describe_domain(domain_key(ranged)),
          "a range domain describes with its name")
    check(describe_domain(domain_key("StatusDomain")) == "StatusDomain name",
          "and a domain arcpy knows only the name of describes without a "
          "count it cannot know")
    check(describe_domain(("codedValue", "", ()))
          == "(unnamed) codedValue",
          "a domain with no name at all still describes")

    # ---- spatial references
    check(sr_key(None) is None, "a side with no spatial reference says so")
    check(sr_key({}) is None, "an empty spatial reference object says so too")
    check(sr_key({"wkid": 2272}) == ("wkid", 2272),
          "a wkid is read")
    check(sr_key(2272) == ("wkid", 2272), "a bare integer wkid is read")
    check(sr_key({"wkid": 102729, "latestWkid": 2272}) == ("wkid", 2272),
          "latestWkid wins over wkid: a service reports pennsylvania state "
          "plane south as Esri's 102729 and EPSG's 2272 together, and only the "
          "second one means anything to anybody else  <-- pinned defect")
    check(sr_key({"wkid": 102729, "latestWkid": 2272})
          != sr_key({"wkid": 102729}),
          "so a side that reports only the Esri code does NOT already answer "
          "the same as one that reports both, which is what makes the "
          "preference a real one  <-- pinned defect")
    check(sr_key({"wkid": 102100, "latestWkid": 3857}) == ("wkid", 3857),
          "and web mercator has one spelling whichever key carries it")
    check(sr_key({"wkid": 102100}) == sr_key({"wkid": 3857}),
          "102100 and 3857 are the same spatial reference, not a reprojection "
          "<-- pinned defect")
    check(sr_key({"wkid": 102113}) == sr_key({"wkid": 3857}),
          "and so is the older 102113")
    check(sr_key({"wkid": 2272}) != sr_key({"wkid": 2271}),
          "two different state plane zones, south and north, are different")
    check(sr_key({"wkt": 'PROJCS["x"]'}) == ("wkt", 'PROJCS["x"]'),
          "a wkt is read when there is no wkid")
    check(sr_key({"wkt": 'PROJCS["x"]'}) != sr_key({"wkt": 'PROJCS["y"]'}),
          "two different wkt strings are two different spatial references")
    check(sr_key({"wkt": 'PROJCS["x"]'})
          == sr_key({"wkt": 'PROJCS["x"]\n   '}),
          "whitespace in a wkt is not a difference  <-- pinned defect")
    check(sr_key({"wkid": 2272, "wkt": 'PROJCS["x"]'}) == ("wkid", 2272),
          "a wkid is preferred over a wkt when both are given")
    check(sr_key({"wkid": 0}) is None,
          "a wkid of 0 is not a spatial reference, it is an unknown one")
    check(sr_key(0) is None,
          "and a bare 0 is held to the same rule, or an unknown spatial "
          "reference spelled two ways reads as a reprojection  "
          "<-- pinned defect")
    check(sr_key('PROJCS["x"]') == ("wkt", 'PROJCS["x"]'),
          "a bare wkt string is read")
    check(sr_key("   ") is None,
          "but a wkt of nothing but whitespace is no wkt, the same as the "
          "object form, or it compares unequal to the side that reported "
          "nothing at all  <-- pinned defect")
    raises(lambda: sr_key(True), "a boolean spatial reference raises")
    raises(lambda: sr_key([2272]), "a list spatial reference raises")

    # ---- subtypes and capabilities
    check(subtype_key({}) is None, "a layer with no subtypes says so")
    check(subtype_key({"subtypeField": "STATUS",
                       "types": [{"id": 1, "name": "Active"},
                                 {"id": 2, "name": "Pending"}]})
          == ("STATUS", (("1", "Active"), ("2", "Pending"))),
          "the rest shape of subtypes is read")
    check(subtype_key({"subtypeFieldName": "status",
                       "subtypes": [{"code": 2, "name": "Pending"},
                                    {"code": 1, "name": "Active"}]})
          == ("STATUS", (("1", "Active"), ("2", "Pending"))),
          "the geodatabase shape is read to the same value, in any order")
    check(subtype_key({"subtypeField": "STATUS", "types": []})
          == ("STATUS", ()),
          "a subtype field with no codes left is not the same as no subtypes")
    raises(lambda: subtype_key({"types": ["Active"]}),
           "a subtype that is a bare string raises")
    raises(lambda: subtype_key({"types": {"id": 1}}),
           "and types that are an object rather than a list raise  "
           "<-- pinned defect")
    check(capability_set("Query,Create,Update")
          == capability_set("Update,Query,Create"),
          "capabilities compare without regard to order  <-- pinned defect")
    check(capability_set("Query, Create") == capability_set("query,create"),
          "and without regard to case or spaces")
    check(capability_set("") == frozenset(), "an empty capabilities string is "
          "an empty set, not a set holding one empty name")
    check(capability_set(["Query", "Create"]) == capability_set("Query,Create"),
          "a list of capabilities reads the same as the string")
    raises(lambda: capability_set(7), "a numeric capabilities value raises")

    # ---- one field, normalized
    field = normalize_field({"name": "OWNER", "type": "esriFieldTypeString",
                             "alias": "Owner Name", "length": 80})
    check(field["name"] == "OWNER", "the name is kept")
    check(field["type"] == "esriFieldTypeString", "the type is canonical")
    check(field["alias"] == "Owner Name", "the alias is kept")
    check(field["length"] == 80, "the length of a text field is kept")
    check(field["role"] == "", "and it carries no identity role")
    check(normalize_field({"name": "OWNER", "type": "String"})["alias"]
          == "OWNER",
          "a field with no alias reports its name as its alias, which is what "
          "arcpy does, so a side that does not report aliases does not read "
          "as a side whose aliases all changed  <-- pinned defect")
    check(normalize_field({"name": "OWNER", "type": "String",
                           "alias": "   "})["alias"] == "OWNER",
          "and so does a field whose alias is blank")
    check(normalize_field({"name": "OWNER", "type": "String",
                           "aliasName": "Owner Name"})["alias"] == "Owner Name",
          "arcpy spells it aliasName and that is read too")
    check(normalize_field({"name": "  OWNER  ",
                           "type": "String"})["name"] == "OWNER",
          "a name with spaces around it is trimmed")
    check(normalize_field({"name": "ACRES", "type": "Double",
                           "length": 8})["length"] is None,
          "the length of a double is dropped: arcpy reports 8 and the service "
          "reports nothing, and comparing them invents a difference  "
          "<-- pinned defect")
    check(normalize_field({"name": "LASTEDIT", "type": "esriFieldTypeDate",
                           "length": 8})["length"] is None,
          "and so is the length of a date field  <-- pinned defect")
    check(normalize_field({"name": "OWNER", "type": "String",
                           "length": 0})["length"] is None,
          "a length of zero is no length")
    check(normalize_field({"name": "OWNER", "type": "String",
                           "length": None})["length"] is None,
          "and neither is a null length")
    check(normalize_field({"name": "OWNER", "type": "String",
                           "length": True})["length"] is None,
          "a length of True is not a length of 1  <-- pinned defect")
    check(normalize_field({"name": "OID", "type": "OID"})["role"] == "oid",
          "an arcpy OID field carries the oid role through the fold")
    check(normalize_field({"name": "S", "type": "String",
                           "domain": coded})["domain"][1] == "StatusDomain",
          "a field domain is read")
    raises(lambda: normalize_field({"type": "String"}),
           "a field with no name raises")
    raises(lambda: normalize_field({"name": "", "type": "String"}),
           "a field with an empty name raises")
    raises(lambda: normalize_field({"name": "OWNER"}),
           "a field with no type raises")
    raises(lambda: normalize_field("OWNER"),
           "a field that is a bare string raises")
    raises(lambda: normalize_field(None), "a null field raises")

    # ---- one layer, normalized
    bare = normalize_layer({}, "bare")
    check(bare["fields"] == [],
          "a layer definition with no fields key at all normalizes to no "
          "fields rather than raising  <-- pinned defect")
    check("geometryType" not in bare,
          "and reports no geometry type, which is different from reporting "
          "an empty one")
    check("maxRecordCount" not in bare, "and no maxRecordCount")
    check("capabilities" not in bare, "and no capabilities")
    check("subtypes" not in bare, "and no subtypes")
    check(normalize_layer({"fields": []})["fields"] == [],
          "an empty field list is an empty field list")
    check(normalize_layer({"fields": None})["fields"] == [],
          "and so is a null one")
    check(normalize_layer({"types": []})["subtypes"] is None,
          "a layer that reports types at all is comparable on subtypes, even "
          "when it has none  <-- pinned defect")
    check(normalize_layer({"capabilities": ""})["capabilities"]
          == frozenset(),
          "a layer that reports no capabilities is still comparable on them")
    check(normalize_layer({"maxRecordCount": 0})["maxRecordCount"] == 0,
          "a maxRecordCount of zero is a value and not an absence")
    check(normalize_layer({"spatialReference": {"wkid": 0}}).get(
        "spatialReference") is None,
          "an unknown spatial reference is left out rather than compared")
    check(layer_spatial_reference({"spatialReference": {"wkid": 2272}})
          == {"wkid": 2272}, "a top level spatial reference is found")
    check(layer_spatial_reference(
        {"extent": {"xmin": 0, "spatialReference": {"wkid": 2272}}})
        == {"wkid": 2272},
          "and so is the one a published layer keeps inside its extent, which "
          "is the only place a real service puts it  <-- pinned defect")
    check(layer_spatial_reference(
        {"spatialReference": {"wkid": 2271},
         "extent": {"spatialReference": {"wkid": 2272}}}) == {"wkid": 2271},
          "the top level one wins when a layer reports both")
    check(layer_spatial_reference({}) is None,
          "a layer that reports neither has none")
    check(layer_spatial_reference({"extent": "everything"}) is None,
          "and an extent that is not an object has none either")
    check(normalize_layer(
        {"extent": {"spatialReference": {"wkid": 2272}}})["spatialReference"]
        == ("wkid", 2272),
          "so a layer read from a real service is comparable on its "
          "projection  <-- pinned defect")
    check(diff_layer_properties(
        normalize_layer({"extent": {"spatialReference": {"wkid": 2272}}}),
        normalize_layer({"spatialReference": {"wkid": 2271}}))[0].kind
        == "SPATIAL_REF_CHANGED",
          "and a feature class in one projection against a service in another "
          "is reported  <-- pinned defect")
    raises(lambda: normalize_layer([]), "a layer that is a list raises")
    raises(lambda: normalize_layer({"fields": {}}),
           "a layer whose fields are an object raises")
    raises(lambda: normalize_layer({"maxRecordCount": "1000"}),
           "a maxRecordCount that is a string raises")
    raises(lambda: normalize_layer({"fields": [
        {"name": "OWNER", "type": "String"},
        {"name": "owner", "type": "String"}]}),
           "a layer with two fields of the same name raises, because no "
           "comparison of it can be trusted  <-- pinned defect")
    check(comparable({"a": 1}, {"a": 2}, "a") is True,
          "a property both sides report is comparable")
    check(comparable({"a": 1}, {}, "a") is False,
          "a property only one side reports is NOT comparable  "
          "<-- pinned defect")
    check(comparable({}, {"a": 1}, "a") is False, "either way round")
    check(comparable({"a": None}, {"a": None}, "a") is True,
          "and a property both sides report as absent is comparable, which is "
          "how a lost subtype is caught")
    shaped_layer = normalize_layer({"fields": [
        {"name": "OBJECTID", "type": "OID"},
        {"name": "Shape", "type": "Geometry"},
        {"name": "OWNER", "type": "String"}]}, "a feature class")
    check(len(shaped_layer["fields"]) == 3,
          "a normalized layer keeps every field the side reported, shape "
          "included")
    check([f["name"] for f in data_fields(shaped_layer)]
          == ["OBJECTID", "OWNER"],
          "but the comparison reads only the fields that hold data  "
          "<-- pinned defect")
    check(data_fields(normalize_layer({"fields": []})) == [],
          "and an empty layer has none of either")
    check(diff_layers(shaped_layer, normalize_layer({"fields": [
        {"name": "OBJECTID", "type": "esriFieldTypeOID"},
        {"name": "OWNER", "type": "esriFieldTypeString"}]}, "a service")) == [],
          "so a feature class that lists its shape and a service that does "
          "not are not reported as differing by a field, which would fire on "
          "every dataset comparison  <-- pinned defect")

    # ---- matching the two field lists up
    def fields_of(*specs):
        return [normalize_field(s) for s in specs]

    left = fields_of({"name": "OBJECTID", "type": "esriFieldTypeOID"},
                     {"name": "OWNER", "type": "String", "length": 80})
    right = fields_of({"name": "OWNER", "type": "String", "length": 80},
                      {"name": "OBJECTID", "type": "esriFieldTypeOID"})
    pairs, only_left, only_right = pair_fields(left, right)
    check(len(pairs) == 2, "two field lists in a different order match up")
    check(only_left == [] and only_right == [],
          "and leave nothing unmatched  <-- pinned defect")
    pairs, only_left, only_right = pair_fields(
        fields_of({"name": "owner", "type": "String"}),
        fields_of({"name": "OWNER", "type": "String"}))
    check(len(pairs) == 1,
          "a field named owner in the source and OWNER in the service is one "
          "field  <-- pinned defect")
    pairs, only_left, only_right = pair_fields(
        fields_of({"name": "OWNER", "type": "String"}), [])
    check(len(only_left) == 1 and not pairs,
          "a field the service does not have is left over on the source side")
    pairs, only_left, only_right = pair_fields(
        [], fields_of({"name": "ZONING", "type": "String"}))
    check(len(only_right) == 1 and not pairs,
          "and one the source does not have is left over on the service side")
    check(pair_fields([], []) == ([], [], []),
          "two empty field lists match up to nothing at all")

    # ---- the identity rescue, which is the false positive on every run
    pairs, only_left, only_right = pair_fields(
        fields_of({"name": "FID", "type": "OID"},
                  {"name": "OWNER", "type": "String"}),
        fields_of({"name": "OBJECTID", "type": "esriFieldTypeOID"},
                  {"name": "OWNER", "type": "String"}))
    check(len(pairs) == 2,
          "a source whose object id is FID and a service whose object id is "
          "OBJECTID have two fields, not four  <-- pinned defect")
    check(only_left == [],
          "so the object id is NOT reported as removed  <-- pinned defect")
    check(only_right == [],
          "and the service's object id is not reported as added  "
          "<-- pinned defect")
    oid_pair = [p for p in pairs if p[0]["role"] == "oid"][0]
    check(oid_pair[1]["name"] == "OBJECTID",
          "the two object id fields are paired with each other")
    pairs, only_left, only_right = pair_fields(
        fields_of({"name": "GLOBALID", "type": "GlobalID"}),
        fields_of({"name": "GlobalID_1", "type": "esriFieldTypeGlobalID"}))
    check(len(pairs) == 1 and not only_left and not only_right,
          "the same rescue applies to the global id field  <-- pinned defect")
    pairs, only_left, only_right = pair_fields(
        fields_of({"name": "OBJECTID", "type": "OID"}),
        fields_of({"name": "OBJECTID", "type": "esriFieldTypeOID"}))
    check(len(pairs) == 1 and not only_left and not only_right,
          "and when both sides name it OBJECTID it matches on the name, with "
          "no rescue needed  <-- pinned defect")
    check(diff_one_field(pairs[0][0], pairs[0][1]) == [],
          "an arcpy OID against an esriFieldTypeOID of the same name reports "
          "nothing at all  <-- pinned defect")
    pairs, only_left, only_right = pair_fields(
        fields_of({"name": "OBJECTID", "type": "OID"}),
        fields_of({"name": "OWNER", "type": "String"}))
    check(len(only_left) == 1 and len(only_right) == 1,
          "an object id with no object id on the other side IS reported as "
          "removed, because it really is gone")
    pairs, only_left, only_right = pair_fields(
        fields_of({"name": "FID", "type": "OID"},
                  {"name": "OID2", "type": "OID"}),
        fields_of({"name": "OBJECTID", "type": "esriFieldTypeOID"}))
    check(len(pairs) == 0,
          "two unmatched object id fields on one side are left alone: there "
          "is nothing to tell them apart with  <-- pinned defect")

    # ---- one field against another
    text80 = normalize_field({"name": "OWNER", "type": "String", "length": 80,
                              "alias": "Owner Name"})
    text40 = normalize_field({"name": "OWNER", "type": "String", "length": 40,
                              "alias": "Owner Name"})
    text120 = normalize_field({"name": "OWNER", "type": "String",
                               "length": 120, "alias": "Owner Name"})
    renamed_alias = normalize_field({"name": "OWNER", "type": "String",
                                     "length": 80, "alias": "Owner"})
    check(diff_one_field(text80, text80) == [],
          "a field against itself reports nothing")
    shrink = diff_one_field(text80, text40)
    check(kinds(shrink) == ["LENGTH_DECREASED"],
          "a text field that got shorter reports one difference")
    check(shrink[0].severity == BREAK,
          "and it is a BREAK, because a value that fits the source is "
          "truncated  <-- pinned defect")
    grow = diff_one_field(text80, text120)
    check(kinds(grow) == ["LENGTH_INCREASED"],
          "a text field that got longer reports one difference")
    check(grow[0].severity == WARNING,
          "and it is a warning, because nothing stops working")
    alias_only = diff_one_field(text80, renamed_alias)
    check(kinds(alias_only) == ["ALIAS_CHANGED"],
          "an alias change on its own is one difference")
    check(alias_only[0].severity == WARNING,
          "and an alias-only change is a WARNING, never a break  "
          "<-- pinned defect")
    check("Owner Name" in alias_only[0].detail
          and "Owner" in alias_only[0].detail,
          "and it names both aliases, so somebody can see which way it went")
    small = normalize_field({"name": "N", "type": "esriFieldTypeSmallInteger"})
    large = normalize_field({"name": "N", "type": "esriFieldTypeInteger"})
    widened = diff_one_field(small, large)
    check(kinds(widened) == ["TYPE_WIDENED"],
          "a small integer republished as an integer is reported as a "
          "widening, not as a type change  <-- pinned defect")
    check(widened[0].severity == WARNING,
          "and a widening is a warning: every value it held still fits")
    narrowed = diff_one_field(large, small)
    check(kinds(narrowed) == ["TYPE_CHANGED"],
          "the same change the other way round is a plain type change")
    check(narrowed[0].severity == BREAK, "and it is a break")
    to_text = diff_one_field(
        normalize_field({"name": "ACRES", "type": "Double"}),
        normalize_field({"name": "ACRES", "type": "String", "length": 32}))
    check(kinds(to_text) == ["TYPE_CHANGED"],
          "a double republished as text reports the type change and NOT also "
          "a length change, which would be the same news twice  "
          "<-- pinned defect")
    domained = diff_one_field(
        normalize_field({"name": "S", "type": "String", "domain": coded}),
        normalize_field({"name": "S", "type": "String"}))
    check(kinds(domained) == ["DOMAIN_REMOVED"], "a lost domain is reported")
    check(domained[0].severity == BREAK,
          "and losing a domain is a break: the picklist and the validation "
          "are gone")
    added_domain = diff_one_field(
        normalize_field({"name": "S", "type": "String"}),
        normalize_field({"name": "S", "type": "String", "domain": coded}))
    check(kinds(added_domain) == ["DOMAIN_ADDED"], "a new domain is reported")
    check(added_domain[0].severity == WARNING, "and it is a warning")
    changed_domain = diff_one_field(
        normalize_field({"name": "S", "type": "String", "domain": coded}),
        normalize_field({"name": "S", "type": "String",
                         "domain": coded_extra}))
    check(kinds(changed_domain) == ["DOMAIN_CHANGED"],
          "a domain that gained a code is reported")
    check("2 code(s)" in changed_domain[0].detail
          and "3 code(s)" in changed_domain[0].detail,
          "and the report says how many codes each side had")
    check(changed_domain[0].severity == WARNING,
          "and a changed domain is a warning, as the README table says: "
          "editing gains or moves a constraint  <-- pinned defect")
    check(diff_one_field(
        normalize_field({"name": "S", "type": "String",
                         "domain": "StatusDomain"}),
        normalize_field({"name": "S", "type": "String", "domain": coded}))
        == [],
          "an arcpy source that knows only the domain name reports no domain "
          "change against the service's full list  <-- pinned defect")
    case_only = diff_one_field(
        normalize_field({"name": "owner", "type": "String", "alias": "owner"}),
        normalize_field({"name": "OWNER", "type": "String", "alias": "owner"}))
    check(case_only == [],
          "a field whose name differs only in case reports nothing  "
          "<-- pinned defect")
    renamed = diff_one_field(
        normalize_field({"name": "FID", "type": "OID"}),
        normalize_field({"name": "OBJECTID", "type": "esriFieldTypeOID"}))
    check(kinds(renamed) == ["IDENTITY_RENAMED"],
          "a rescued identity pair reports the rename and nothing else  "
          "<-- pinned defect")
    check(renamed[0].severity == WARNING,
          "and a renamed object id is a warning, not a break")
    check("FID" in renamed[0].detail and "OBJECTID" in renamed[0].detail,
          "and it names both spellings")
    check("ALIAS_CHANGED" not in kinds(renamed),
          "the alias that came with the rename is not reported a second time  "
          "<-- pinned defect")

    # ---- the two fixtures, with exactly six seeded differences
    SOURCE = {
        "name": "Parcels",
        "geometryType": "esriGeometryPoint",
        "maxRecordCount": 1000,
        "capabilities": "Query,Create,Update",
        "spatialReference": {"wkid": 2272},
        "types": [],
        "fields": [
            {"name": "OBJECTID", "type": "esriFieldTypeOID",
             "alias": "OBJECTID"},
            {"name": "PARCELID", "type": "esriFieldTypeString",
             "alias": "Parcel ID", "length": 24},
            {"name": "OWNER", "type": "esriFieldTypeString",
             "alias": "Owner Name", "length": 80},
            {"name": "ACRES", "type": "esriFieldTypeDouble", "alias": "Acres"},
            {"name": "STATUS", "type": "esriFieldTypeString",
             "alias": "Status", "length": 16},
            {"name": "LASTEDIT", "type": "esriFieldTypeDate",
             "alias": "Last Edited"},
        ],
    }
    SERVICE = {
        "name": "Parcels",
        "geometryType": "esriGeometryPoint",
        "maxRecordCount": 2000,                      # 1. maxRecordCount
        "capabilities": "Query,Create,Update",
        "spatialReference": {"wkid": 2272},
        "types": [],
        "fields": [
            {"name": "OBJECTID", "type": "esriFieldTypeOID",
             "alias": "OBJECTID"},
            {"name": "PARCELID", "type": "esriFieldTypeString",
             "alias": "Parcel Number", "length": 24},          # 2. alias
            {"name": "OWNER", "type": "esriFieldTypeString",
             "alias": "Owner Name", "length": 40},             # 3. length
            {"name": "ACRES", "type": "esriFieldTypeString",
             "alias": "Acres", "length": 32},                  # 4. type
            {"name": "LASTEDIT", "type": "esriFieldTypeDate",
             "alias": "Last Edited"},                          # 5. STATUS gone
            {"name": "ZONING", "type": "esriFieldTypeString",
             "alias": "Zoning", "length": 10},                 # 6. ZONING new
        ],
    }
    source = normalize_layer(SOURCE, "the source")
    service = normalize_layer(SERVICE, "the service")
    found = diff_layers(source, service)

    check(len(found) == 6,
          "the two fixtures differ in exactly six ways and all six are found")
    check(kinds(found) == ["ALIAS_CHANGED", "FIELD_ADDED", "FIELD_REMOVED",
                           "LENGTH_DECREASED", "MAX_RECORD_COUNT_CHANGED",
                           "TYPE_CHANGED"],
          "and they are the six that were seeded, with no seventh")
    check(one(found, "FIELD_REMOVED", "STATUS") is not None,
          "STATUS is in the source and not in the service")
    check(one(found, "FIELD_REMOVED", "STATUS").severity == BREAK,
          "and a field the service does not have is a BREAK  "
          "<-- pinned defect")
    check(one(found, "FIELD_ADDED", "ZONING") is not None,
          "ZONING is in the service and not in the source")
    check(one(found, "FIELD_ADDED", "ZONING").severity == WARNING,
          "and a field the source does not have is a WARNING  "
          "<-- pinned defect")
    check(one(found, "TYPE_CHANGED", "ACRES").severity == BREAK,
          "ACRES changed from a double to a string, which is a break")
    check(one(found, "LENGTH_DECREASED", "OWNER").severity == BREAK,
          "OWNER lost 40 characters of length, which is a break")
    check(one(found, "ALIAS_CHANGED", "PARCELID").severity == WARNING,
          "PARCELID's alias changed, which is a warning")
    check(one(found, "MAX_RECORD_COUNT_CHANGED", "maxRecordCount") is not None,
          "and maxRecordCount moved from 1000 to 2000")
    check("1000" in one(found, "MAX_RECORD_COUNT_CHANGED",
                        "maxRecordCount").detail,
          "with both numbers in the report")
    check(counts(found) == (3, 3), "three breaks and three warnings")
    check(verdict(found) == "BREAK", "so the verdict is BREAK")
    check(one(found, "FIELD_REMOVED", "OBJECTID") is None,
          "and the object id is not among them  <-- pinned defect")

    # ---- identical input, and the direction it is read in
    check(diff_layers(source, source) == [],
          "a layer against itself reports nothing")
    check(diff_layers(service, service) == [],
          "and so does the other one")
    check(diff_layers(source, normalize_layer(SOURCE, "again")) == [],
          "two readings of the same definition report nothing, so a run that "
          "changed nothing prints nothing  <-- pinned defect")
    again = diff_layers(source, service)
    check(again == found,
          "running the comparison twice gives the same answer  "
          "<-- pinned defect")
    swapped = diff_layers(service, source)
    check(len(swapped) == 6, "swapping the sides still finds six differences")
    check(sorted(d.target for d in swapped if d.kind == "FIELD_ADDED")
          == sorted(d.target for d in found if d.kind == "FIELD_REMOVED"),
          "and every removal becomes an addition  <-- pinned defect")
    check(sorted(d.target for d in swapped if d.kind == "FIELD_REMOVED")
          == sorted(d.target for d in found if d.kind == "FIELD_ADDED"),
          "and every addition becomes a removal  <-- pinned defect")
    check(one(swapped, "LENGTH_INCREASED", "OWNER") is not None,
          "a length that fell one way rises the other way")
    check(counts(swapped) == (2, 4),
          "and the severities move with them: the removal that was a break is "
          "now an addition that is a warning")
    reordered = dict(SOURCE)
    reordered["fields"] = list(reversed(SOURCE["fields"]))
    check(diff_layers(normalize_layer(reordered, "the source"), service)
          == found,
          "reading the source's fields in the opposite order gives a report "
          "that is identical byte for byte  <-- pinned defect")
    check(diff_layers(normalize_layer(reordered, "x"),
                      normalize_layer(SOURCE, "y")) == [],
          "and a field list in a different order is not a difference at all  "
          "<-- pinned defect")

    # ---- an empty side
    empty = normalize_layer({"fields": []}, "empty")
    gone = diff_layers(source, empty)
    check(len(gone) == 6, "a service with no fields loses all six of them")
    check(set(kinds(gone)) == set(["FIELD_REMOVED"]),
          "and every one is a removal")
    check(exit_code(gone) == 1, "which fails")
    check(len(diff_layers(empty, source)) == 6,
          "an empty source reports all six as additions")
    check(exit_code(diff_layers(empty, source)) == 0,
          "and additions alone do not fail  <-- pinned defect")
    check(diff_layers(empty, empty) == [],
          "two empty layers are the same layer")
    check(diff_layers(normalize_layer({}), normalize_layer({})) == [],
          "and so are two layers with no fields key at all  "
          "<-- pinned defect")
    check(len(diff_layers(source, normalize_layer({}, "no key"))) == 6,
          "a layer definition with no fields key is compared as having none, "
          "rather than as having whatever the other side has  "
          "<-- pinned defect")

    # ---- what the layer is, rather than what it holds
    def layer(**over):
        spec = {"fields": [], "geometryType": "esriGeometryPoint",
                "maxRecordCount": 1000, "capabilities": "Query,Create",
                "spatialReference": {"wkid": 2272}, "types": []}
        spec.update(over)
        return normalize_layer(spec, "x")

    base = layer()
    check(diff_layer_properties(base, base) == [],
          "a layer's properties against themselves report nothing")
    geom = diff_layer_properties(base, layer(
        geometryType="esriGeometryPolygon"))
    check(kinds(geom) == ["GEOMETRY_TYPE_CHANGED"],
          "a point layer republished as polygons is reported")
    check(geom[0].severity == BREAK,
          "and it is a break: every symbology rule stops")
    check("esriGeometryPoint" in geom[0].detail
          and "esriGeometryPolygon" in geom[0].detail,
          "with both geometry types named")
    proj = diff_layer_properties(base, layer(spatialReference={"wkid": 2271}))
    check(kinds(proj) == ["SPATIAL_REF_CHANGED"],
          "a layer republished in another projection is reported")
    check(proj[0].severity == BREAK, "and it is a break")
    check("wkid 2272" in proj[0].detail, "with the wkid it used to be in")
    check(diff_layer_properties(
        base, layer(spatialReference={"wkid": 2272, "latestWkid": 2272}))
        == [],
          "and a service that reports latestWkid as well is not a change  "
          "<-- pinned defect")
    caps = diff_layer_properties(base, layer(capabilities="Query"))
    check(kinds(caps) == ["CAPABILITY_REMOVED"],
          "a service that stopped allowing Create is reported")
    check(caps[0].severity == BREAK,
          "and a lost capability is a break: the editing app stops saving")
    check("CREATE" in caps[0].detail, "with the capability named")
    caps = diff_layer_properties(base, layer(capabilities="Query,Create,Delete"))
    check(kinds(caps) == ["CAPABILITY_ADDED"],
          "a service that now allows Delete is reported")
    check(caps[0].severity == WARNING,
          "and a gained capability is a warning, not a break")
    both = diff_layer_properties(base, layer(capabilities="Query,Delete"))
    check(kinds(both) == ["CAPABILITY_ADDED", "CAPABILITY_REMOVED"],
          "one capability gained and one lost is two differences")
    check(diff_layer_properties(base, layer(capabilities="Create,Query")) == [],
          "the same capabilities in another order are no difference  "
          "<-- pinned defect")
    rec = diff_layer_properties(base, layer(maxRecordCount=2000))
    check(rec[0].severity == WARNING,
          "a maxRecordCount change is a warning: it is a paging question, not "
          "a schema break  <-- pinned defect")
    subs = diff_layer_properties(
        layer(subtypeField="STATUS", types=[{"id": 1, "name": "Active"}]),
        layer())
    check(kinds(subs) == ["SUBTYPES_REMOVED"],
          "a service that lost its subtypes is reported")
    check(subs[0].severity == BREAK,
          "and losing subtypes is a break: the editing templates go with them")
    subs = diff_layer_properties(
        layer(subtypeField="STATUS", types=[{"id": 1, "name": "Active"}]),
        layer(subtypeField="STATUS", types=[{"id": 1, "name": "Active"},
                                            {"id": 2, "name": "Pending"}]))
    check(kinds(subs) == ["SUBTYPES_CHANGED"], "a new subtype code is reported")
    check(subs[0].severity == WARNING, "and it is a warning")
    check("1 code(s)" in subs[0].detail and "2 code(s)" in subs[0].detail,
          "with a count of the codes on each side")

    # ---- a property only one side reports is never a difference
    partial = normalize_layer({"fields": []}, "a dataset")
    check(diff_layer_properties(base, partial) == [],
          "a source that reports no capabilities, no maxRecordCount and no "
          "geometry type produces no layer differences at all, which is what "
          "an arcpy-read table looks like  <-- pinned defect")
    check(diff_layer_properties(partial, base) == [],
          "and the same the other way round  <-- pinned defect")
    check(diff_layers(partial, base) == [],
          "so a comparison against a side that knows less is silent rather "
          "than wrong  <-- pinned defect")

    check(property_text(None) == "none", "an absent property describes as none")
    check(property_text(("wkid", 2272)) == "wkid 2272", "a wkid describes")
    check("wkt" in property_text(("wkt", "PROJCS")), "a wkt describes")
    check(property_text(1000) == "1000", "a number describes as itself")
    check(property_text(("STATUS", (("1", "A"),)))
          == "STATUS with 1 code(s)", "a subtype key describes")

    # ---- the verdict, and what --strict changes
    break_only = [Difference("FIELD_REMOVED", BREAK, "STATUS", "")]
    warn_only = [Difference("ALIAS_CHANGED", WARNING, "OWNER", "")]
    check(verdict([]) == "MATCH", "no differences is a MATCH")
    check(verdict(warn_only) == "WARN", "warnings alone are a WARN")
    check(verdict(break_only) == "BREAK", "one break is a BREAK")
    check(verdict(warn_only + break_only) == "BREAK",
          "and one break among twenty warnings is still a BREAK")
    check(counts([]) == (0, 0), "nothing counts as nothing")
    check(counts(warn_only + break_only) == (1, 1), "one of each counts")
    check(exit_code([]) == 0, "a matching schema exits 0")
    check(exit_code(warn_only) == 0,
          "warnings alone exit 0, so a run does not fail on an alias  "
          "<-- pinned defect")
    check(exit_code(break_only) == 1, "a break exits 1")
    check(exit_code(warn_only, strict=True) == 1,
          "--strict makes a warning fail too  <-- pinned defect")
    check(exit_code([], strict=True) == 0,
          "but --strict on a matching schema still exits 0")
    check(exit_code(break_only, strict=True) == 1,
          "and --strict on a break is still 1")
    check(exit_code(found) == 1,
          "the six seeded differences fail, because three of them are breaks")
    check(exit_code([d for d in found if d.severity == WARNING]) == 0,
          "their three warnings on their own do not")
    check(exit_code([d for d in found if d.severity == WARNING], strict=True)
          == 1, "until --strict")

    # ---- the order of the report
    jumbled = [Difference("ALIAS_CHANGED", WARNING, "zebra", "a"),
               Difference("FIELD_REMOVED", BREAK, "owner", "b"),
               Difference("FIELD_ADDED", WARNING, "acres", "c"),
               Difference("TYPE_CHANGED", BREAK, "acres", "d")]
    ordered = sort_differences(jumbled)
    check([d.severity for d in ordered]
          == [BREAK, BREAK, WARNING, WARNING],
          "breaks are printed before warnings, so the line that matters is "
          "at the top  <-- pinned defect")
    check([d.target for d in ordered] == ["acres", "owner", "acres", "zebra"],
          "and within a severity they are ordered by what they are about")
    check(sort_differences(list(reversed(jumbled))) == ordered,
          "shuffling the input does not change the order of the report  "
          "<-- pinned defect")
    check(sort_differences([]) == [], "nothing sorts to nothing")

    # ---- the report a person reads
    lines = describe(found, "parcels.json", "https://gis/x/0", source, service)
    text = "\n".join(lines)
    check(lines[0] == "svcdrift: source -> service",
          "the report opens by saying which way round it read the two sides")
    check("parcels.json" in text, "it names the source")
    check("https://gis/x/0" in text, "and the service")
    check("6 field(s)" in text, "and how many fields each side had")
    check(text.count("BREAK   ") == 3, "three lines are marked BREAK")
    check(text.count("WARNING ") == 3, "and three are marked WARNING")
    check("VERDICT: BREAK" in text, "and the last line is the verdict")
    check("3 break(s), 3 warning(s)" in text, "with the two counts above it")
    check("--strict" not in text,
          "and no mention of --strict, which was not passed")
    strict_text = "\n".join(describe(found, "a", "b", source, service,
                                     strict=True))
    check("--strict makes a warning fail" in strict_text,
          "a --strict run says so in the report, so the exit code is "
          "explainable  <-- pinned defect")
    clean = "\n".join(describe([], "a", "b", source, source))
    check("no differences" in clean,
          "a report with nothing in it says so rather than printing a blank")
    check("VERDICT: MATCH" in clean, "and its verdict is MATCH")
    no_source = "\n".join(describe([], "a", "b", None, service))
    check("the service was read on its own" in no_source,
          "a probe-only run says there was no source to compare against")
    probed_text = "\n".join(describe([], "a", "b", source, service, probed=4))
    check("probe: 4 feature(s)" in probed_text,
          "a probed run says how many features it read, so an empty layer is "
          "not read as a clean bill of health  <-- pinned defect")
    check("STATUS" in text and "ZONING" in text and "OWNER" in text
          and "ACRES" in text and "PARCELID" in text,
          "every field the comparison found something about is named")

    # ---- the report a script reads
    document = build_report(found, "parcels.json", "https://gis/x/0",
                            source, service)
    check(document["verdict"] == "BREAK", "the json report carries the verdict")
    check(document["exitCode"] == 1, "and the exit code the run will use")
    check(document["breaks"] == 3 and document["warnings"] == 3,
          "and both counts")
    check(len(document["differences"]) == 6, "and all six differences")
    check(document["differences"][0]["severity"] == BREAK,
          "in the same order as the text report")
    check(sorted(document["differences"][0].keys())
          == ["detail", "kind", "severity", "target"],
          "each one carrying the four fields a script filters on")
    check(document["sourceFieldCount"] == 6 and
          document["serviceFieldCount"] == 6, "with both field counts")
    check(document["svcdrift"] == VERSION,
          "and the version of the tool that wrote it")
    check(json.loads(json.dumps(document)) == document,
          "and the whole document survives a round trip through json  "
          "<-- pinned defect")
    check(build_report([], "a", "b", None, service)["source"] is None,
          "a probe-only document reports no source")
    check(build_report([], "a", "b", None, service)["sourceFieldCount"] is None,
          "and no source field count")
    check(build_report([], "a", "b", source, service,
                       strict=True)["strict"] is True,
          "and a --strict run says so in the document")
    check(build_report([], "a", "b", source, service, probed=0)["probedFeatures"]
          == 0, "and a probe that read no features records the zero")

    # ---- the advertised schema against what a query returns
    returned = [{"attributes": {"OBJECTID": 1, "PARCELID": "x",
                                "OWNER": "y", "ACRES": 1.0,
                                "LASTEDIT": 0, "ZONING": "R1"}}]
    check(probe_fields(service, returned) == [],
          "a service that returns everything it advertises reports nothing")
    dropped = [{"attributes": {"OBJECTID": 1, "PARCELID": "x",
                               "ACRES": 1.0, "LASTEDIT": 0, "ZONING": "R1"}}]
    missing = probe_fields(service, dropped)
    check(kinds(missing) == ["FIELD_NOT_RETURNED"],
          "a field the layer advertises and the data does not carry is found")
    check(missing[0].target == "OWNER", "and it is named")
    check(missing[0].severity == BREAK,
          "and it is a break: every client believes the definition  "
          "<-- pinned defect")
    extra = probe_fields(service, [{"attributes": {
        "OBJECTID": 1, "PARCELID": "x", "OWNER": "y", "ACRES": 1.0,
        "LASTEDIT": 0, "ZONING": "R1", "SURPRISE": 2}}])
    check(kinds(extra) == ["FIELD_NOT_ADVERTISED"],
          "a field the data carries and the definition does not list is found")
    check(extra[0].severity == WARNING, "and it is a warning")
    check(probe_fields(service, []) == [],
          "a layer that returned no rows is not reported as having lost every "
          "field it has  <-- pinned defect")
    check(probe_fields(service, [{"attributes": {}}])
          == probe_fields(service, [{}]),
          "a feature with no attributes key reads the same as one with none")
    check(len(probe_fields(service, [{}])) == 6,
          "and a row with no attributes at all loses all six fields")
    raises(lambda: probe_fields(service, [None]),
           "a feature of null raises a ValueError, not an AttributeError "
           "traceback that exits 1  <-- pinned defect")
    raises(lambda: probe_fields(service, [{"attributes": ["OWNER"]}]),
           "and so do attributes that are a list  <-- pinned defect")
    check(probe_fields(service, [{"attributes": {"objectid": 1}},
                                 {"attributes": {"PARCELID": "x"}}])
          != probe_fields(service, [{"attributes": {"objectid": 1}}]),
          "the fields of every row read are taken together")
    lowered = probe_fields(
        normalize_layer({"fields": [{"name": "OWNER", "type": "String"}]}),
        [{"attributes": {"owner": "y"}}])
    check(lowered == [],
          "an attribute that comes back in another case is the same field  "
          "<-- pinned defect")
    shaped = probe_fields(
        normalize_layer({"fields": [
            {"name": "OBJECTID", "type": "esriFieldTypeOID"},
            {"name": "Shape", "type": "esriFieldTypeGeometry"}]}),
        [{"attributes": {"OBJECTID": 1}}])
    check(shaped == [],
          "the geometry field is never an attribute, so a layer is not "
          "reported as having lost its own shape  <-- pinned defect")

    # ---- symbology: numbers, and one spelling for a drawingInfo
    check(is_number(3) and is_number(2.5), "an int and a float are numbers")
    check(not is_number(True),
          "a boolean is not a number, although python counts it as one  "
          "<-- pinned defect")
    check(not is_number("3"), "and neither is a numeric string")
    check(numbers_close(1234.56, 1234.5600000000001),
          "a class break written back with float noise on it is the same "
          "number  <-- pinned defect")
    check(numbers_close(0.7, 0.7000000001),
          "and so is a width that moved by less than the tolerance")
    check(not numbers_close(0.7, 0.7000001),
          "while a width that moved by a ten-millionth is a change: the "
          "tolerance holds nothing a person could type")
    check(not numbers_close(0.7, 0.8), "a width that moved by a tenth is not")
    check(not numbers_close(100000.0, 100001.0),
          "at 1:100000 a step of one is ten thousand times the tolerance, "
          "so it is a change")
    check(numbers_close(100000.0, 100000.00000000001),
          "while noise in the last digit a double holds at that size is noise")
    check(not numbers_close(100000.0, 100000.00001),
          "and a hundred-thousandth of a unit, which a person can type, is a "
          "change")
    check(not numbers_close(10000000000, 10000000009)
          and not numbers_close(123456789012, 123456789100),
          "a break edited from 10000000000 to 10000000009, or from "
          "123456789012 to 123456789100, is a change, where a relative "
          "tolerance of 1e-9 read both edits as noise  <-- pinned defect")
    check(numbers_close(123456789012.0, 123456789012.00002),
          "while float noise on a number that size is still noise")
    check(not numbers_close(0.0, 0.000002),
          "below one the tolerance is absolute, so two millionths is a change")
    check(not numbers_close(float("inf"), 1e300),
          "an infinity is close to nothing but itself  <-- pinned defect")
    check(not numbers_close(10 ** 400, 1e300)
          and numbers_close(10 ** 400, 10 ** 400),
          "an integer too large for a float is compared without an "
          "OverflowError  <-- pinned defect")
    check(not numbers_close(1000000, 1000001)
          and not numbers_close(1000001, 1000000),
          "a class break edited from 1000000 to 1000001 is a change  "
          "<-- pinned defect")
    check(not numbers_close(1000000.0, 1000001)
          and not numbers_close(1234567.89, 1234568),
          "and so is the same edit when the server wrote the break as "
          "1000000.0, or a break of 1234567.89 rounded to 1234568  "
          "<-- pinned defect")
    check(numbers_close(1000000, 1000000.0000001),
          "while an integer against a float with noise on it is still noise")
    check(numbers_close(float("inf"), float("inf")),
          "and it is equal to itself")
    check(numbers_close(float("nan"), float("nan")),
          "a NaN, which json.loads reads, is the same as another NaN, or a "
          "layer holding one never equals its own snapshot  <-- pinned defect")
    check(not numbers_close(float("nan"), 0)
          and not numbers_close(1, float("nan")),
          "and a NaN is close to no number, on either side")
    check(numbers_close(0, 1e-9),
          "below 1 the tolerance is 1e-9 outright, not 1e-9 of the larger "
          "number, so a width of 0 written back as 1e-9 has not moved  "
          "<-- pinned defect")
    check(same_value(True, True) and not same_value(True, 1),
          "true is not the number 1  <-- pinned defect")
    check(same_value(0, 0.0), "0 and 0.0 are the same number")
    check(not same_value("1", 1),
          "outside a class value, a string is not the number it spells")
    check(same_value(None, None), "nothing is nothing")

    check(canonical_drawing({"color": [255, 0, 0]})
          == {"color": [255, 0, 0, 255]},
          "a colour with no alpha is opaque, the same as one that says 255  "
          "<-- pinned defect")
    check(canonical_drawing({"haloColor": [254.9999, 0, 0.0001, 255]})
          == {"haloColor": [255, 0, 0, 255]},
          "a colour channel is a whole number, so float noise on it is gone")
    check(canonical_drawing({"color": [float("inf"), 0, 0]})
          == {"color": [float("inf"), 0, 0]},
          "a colour holding an infinity is left as it stands rather than "
          "rounded, which raised an OverflowError  <-- pinned defect")
    check(canonical_drawing({"color": [300, 0, 0]}) == {"color": [300, 0, 0]},
          "and so is a channel outside 0 to 255, which is not a colour")
    check(canonical_drawing({"color": [255, 0]}) == {"color": [255, 0]},
          "two numbers under a colour key are not a colour and are left alone")
    check(canonical_drawing({"color": [1.4, 2, 3, 4, 5]})
          == {"color": [1.4, 2, 3, 4, 5]},
          "and nor are five, so their float noise is not rounded away")
    check(canonical_drawing({"color": ["red", 0, 0]})
          == {"color": ["red", 0, 0]},
          "and neither is a list holding a name")
    check(canonical_drawing({"stops": [1, 2, 3]}) == {"stops": [1, 2, 3]},
          "three numbers under a key that is not a colour are not given an "
          "alpha  <-- pinned defect")
    check(canonical_drawing({"color": None, "outline": None}) == {},
          "a null is the same as a key that is not there")
    check(canonical_drawing({"angle": 0, "xoffset": 0.0, "yoffset": 0,
                             "size": 8}) == {"size": 8},
          "a default the server spelled out is the same as one it left out  "
          "<-- pinned defect")
    check(canonical_drawing({"angle": 15}) == {"angle": 15},
          "but an angle that is not the default is kept")
    check(canonical_drawing({"kerning": True, "rightToLeft": False}) == {},
          "the text symbol defaults go the same way")
    check(canonical_drawing({"kerning": 1}) == {"kerning": 1},
          "and a default is matched by type as well as value, so a 1 is not "
          "taken for true  <-- pinned defect")
    check(canonical_drawing({"authoringInfo": {"classificationMethod": "x"},
                             "classificationMethod": "esriClassifyManual",
                             "type": "simple"}) == {"type": "simple"},
          "authoringInfo records how a renderer was made and not what it "
          "draws, and is dropped  <-- pinned defect")
    check(canonical_drawing({"visualVariables": [], "outline": {}}) == {},
          "an empty list and an empty object are the same as nothing")
    check(canonical_drawing({"outline": {"color": None}}) == {},
          "including an object that is empty once its nulls are gone")
    check(canonical_drawing({"type": "esriPMS", "url": "471E7E31",
                             "imageData": "iVBOR"})
          == {"type": "esriPMS", "imageData": "iVBOR"},
          "a picture symbol is its image, not the name the server filed it "
          "under  <-- pinned defect")
    check(canonical_drawing({"type": "esriPMS", "url": "http://x/a.png"})
          == {"type": "esriPMS", "url": "http://x/a.png"},
          "and one with only a url keeps it, because the url is all there is")
    check(canonical_drawing({"expression": "  $feature.OWNER\r\n"})
          == {"expression": "$feature.OWNER"},
          "an expression is read without the line ending and the edge spaces "
          "an editor added  <-- pinned defect")
    check(canonical_drawing({"valueExpression": "var a = 1;\r\nreturn a;"})
          == {"valueExpression": "var a = 1;\nreturn a;"},
          "and a line ending inside a multi-line expression is folded too, "
          "which strip() alone never reaches  <-- pinned defect")
    check(canonical_drawing({"value": " A "}) == {"value": " A "},
          "but a class value keeps its spaces, because a value with a space "
          "in it is a different value  <-- pinned defect")
    check(canonical_drawing({"where": "   "}) == {},
          "a where clause of nothing but spaces is no where clause")
    check(canonical_drawing([{"a": None}, None]) == [{}, None],
          "a list keeps its positions, nulls included")
    check(canonical_drawing({"stops": [{"value": 9, "size": 2},
                                       {"value": 1, "size": 1}]})
          == {"stops": [{"value": 1, "size": 1}, {"value": 9, "size": 2}]},
          "the stops of a visual variable are put in the order of their "
          "values, which is where they sit on the ramp  <-- pinned defect")
    check(canonical_drawing({"stops": [{"value": 5, "color": [0, 0, 0]},
                                       {"value": 5, "color": [9, 9, 9]}]})
          ["stops"][0]["color"] == [0, 0, 0, 255],
          "and two stops on one value keep their order, which is the hard "
          "edge they make")
    check(canonical_drawing({"stops": [{"value": "b"}, {"value": "a"}]})
          == {"stops": [{"value": "b"}, {"value": "a"}]}
          and canonical_drawing({"stops": [{"value": float("nan")},
                                           {"value": 1}]})["stops"][1]
          == {"value": 1},
          "while stops that are not all numbers, a NaN included, keep their "
          "positions")
    check([stop["value"] for stop in canonical_drawing({"stops": [
        {"value": 2}, {"value": 1}, {"value": float("nan")}]})["stops"]][:2]
          == [2, 1],
          "including a NaN listed last, which a sort would move to the middle")
    check(canonical_drawing("x") == "x" and canonical_drawing(3) == 3,
          "and a plain value is itself")

    check(first_mismatch({"a": 1, "b": [1, 2]},
                         {"b": [1, 2], "a": 1.0000000001}) is None,
          "two objects with the same content in another key order match  "
          "<-- pinned defect")
    check(first_mismatch({"s": {"stops": [1, 2]}}, {"s": {"stops": [1, 3]}})
          == ("s.stops[1]", 2, 3),
          "the first difference is found, and its path is named")
    check(first_mismatch({"s": {"color": [1, 2, 3, 255]}},
                         {"s": {"color": [1, 2, 4, 255]}})
          == ("s.color", [1, 2, 3, 255], [1, 2, 4, 255]),
          "but a colour is reported whole, not as the one channel that moved  "
          "<-- pinned defect")
    check(first_mismatch([1, 2], [1, 2, 3])[0] == "(the whole value)",
          "two lists of different lengths differ as a whole")
    check(first_mismatch({"a": 1}, {}) == ("a", 1, None),
          "a key only one side has is a difference with nothing on the other")
    check(first_mismatch({"a": True}, {"a": 1}) is not None,
          "and true against 1 is a difference")
    check(value_text(None) == "unset",
          "an absent value prints as unset, which is also true of a default")
    check(value_text([255, 0, 0, 255]) == "[255, 0, 0, 255]",
          "a value prints as json")
    check(len(value_text("x" * 200)) == 60
          and value_text("x" * 200).endswith("..."),
          "and a long one is cut to sixty characters, so one line stays one "
          "line")
    check(class_value(1) == class_value(1.0) == class_value("1") == "1",
          "a class value of 1, 1.0 or \"1\" is one value, because a republish "
          "writes a number back as text  <-- pinned defect")
    check(class_value(1.5) == "1.5", "a fraction keeps its fraction")
    check(class_value(0.1 + 0.2) == class_value(0.3) == "0.3",
          "a class value of 0.3 written back as 0.30000000000000004 is still "
          "0.3  <-- pinned defect")
    check(class_value(0.3000001) != class_value(0.3),
          "but a class value is a key the server matches exactly, so a "
          "difference a double really holds is kept")
    check(class_value(10 ** 20) == "100000000000000000000",
          "and a large integer is written out whole")
    check(class_value(1e16) == class_value(10 ** 16) == "10000000000000000",
          "and a large whole float is the same class as the integer, not "
          "1e+16")
    check(class_value(True) == "True", "and a boolean is not taken for 1")
    raises(lambda: as_list("x", "a thing"), "a list that is a string raises")
    raises(lambda: as_object([], "a thing"), "an object that is a list raises")

    # ---- symbology: the fixtures
    def sfs(colour, width=0.7):
        return {"type": "esriSFS", "style": "esriSFSSolid", "color": colour,
                "outline": {"type": "esriSLS", "style": "esriSLSSolid",
                            "color": [110, 110, 110, 255], "width": width}}

    STYLED = {
        "renderer": {
            "type": "uniqueValue", "field1": "STATUS", "fieldDelimiter": ",",
            "defaultSymbol": sfs([130, 130, 130, 255]),
            "defaultLabel": "Other",
            "uniqueValueInfos": [
                {"value": "A", "label": "Active",
                 "symbol": sfs([56, 168, 0, 255])},
                {"value": "P", "label": "Pending",
                 "symbol": sfs([255, 170, 0, 255])},
                {"value": "X", "label": "Expired",
                 "symbol": sfs([168, 0, 0, 255])}]},
        "transparency": 0, "scaleSymbols": True,
        "labelingInfo": [{
            "labelExpressionInfo": {"expression": "$feature.OWNER"},
            "labelPlacement": "esriServerPolygonPlacementAlwaysHorizontal",
            "minScale": 5000, "maxScale": 0,
            "symbol": {"type": "esriTS", "color": [0, 0, 0, 255],
                       "font": {"family": "Arial", "size": 8}}}]}

    def jumble(value):
        """The same JSON with every key order and every class list reversed."""
        if isinstance(value, dict):
            return dict((k, jumble(value[k])) for k in reversed(list(value)))
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return [jumble(item) for item in reversed(value)]
        return value

    # A republish that changes nothing a reader can see.
    republished = jumble(STYLED)
    again_infos = republished["renderer"]["uniqueValueInfos"]
    again_infos[0]["symbol"]["color"] = [168, 0, 0]
    again_infos[1]["symbol"]["outline"]["width"] = 0.7000000000000001
    again_infos[2]["symbol"]["angle"] = 0
    again_infos[2]["symbol"]["xoffset"] = 0.0
    again_infos[2]["description"] = ""
    republished["renderer"]["field1"] = "status"
    republished["renderer"]["fieldDelimiter"] = ", "
    republished["renderer"]["authoringInfo"] = {
        "classificationMethod": "esriClassifyManual"}
    republished["renderer"]["uniqueValueGroups"] = [{"classes": [
        {"values": [["A"]], "label": "Active",
         "symbol": sfs([56, 168, 0, 255])}]}]
    republished["renderer"]["defaultSymbol"]["color"] = [130, 130, 130]
    republished["labelingInfo"][0]["labelExpressionInfo"]["expression"] = \
        "$feature.OWNER\r\n"
    republished["labelingInfo"][0]["symbol"]["kerning"] = True
    republished["labelingInfo"][0]["where"] = None
    check(json.dumps(republished) != json.dumps(STYLED),
          "the republished drawingInfo differs from the original as text")
    check(diff_drawing(drawing_key(STYLED), drawing_key(republished)) == [],
          "but a republish that reverses every key and every class list, "
          "drops an alpha, adds float noise, spells defaults out, changes the "
          "case of the field and adds authoringInfo is not drift  "
          "<-- pinned defect")
    check(diff_drawing(drawing_key(republished), drawing_key(STYLED)) == [],
          "and neither is it read the other way round")
    check(drawing_key(STYLED) == drawing_key(json.loads(json.dumps(STYLED))),
          "a drawingInfo that went through json and back is itself")
    # Spelled out here rather than read from DRAWING_DEFAULTS, so a default
    # deleted from that table turns this red instead of shrinking the loop.
    for key, value in (("angle", 0), ("xoffset", 0), ("yoffset", 0),
                       ("transparency", 0), ("minScale", 0), ("maxScale", 0),
                       ("kerning", True), ("rightToLeft", False),
                       ("decoration", "none"), ("style", "normal"),
                       ("weight", "normal"), ("label", ""),
                       ("description", ""), ("defaultLabel", ""),
                       ("where", ""), ("labelExpression", ""),
                       ("valueExpression", "")):
        check(canonical_drawing({key: value, "size": 8}) == {"size": 8},
              "a %s of %s spelled out is the default a server fills in, not "
              "drift  <-- pinned defect" % (key, json.dumps(value)))
    plain = {"type": "simple", "symbol": sfs([1, 2, 3])}
    check(diff_drawing(drawing_key({"renderer": plain, "transparency": 0}),
                       drawing_key({"renderer": plain})) == [],
          "a drawingInfo that spells out a transparency of 0 is the same as "
          "one that leaves it out  <-- pinned defect")
    one_class = {"type": "uniqueValue", "field1": "STATUS",
                 "uniqueValueInfos": [{"value": "A", "symbol": sfs([1, 2, 3])}]}
    check(diff_drawing(drawing_key({"renderer": one_class}), drawing_key(
        {"renderer": dict(one_class, uniqueValueInfos=[
            {"value": "A", "label": "", "symbol": sfs([1, 2, 3])}])})) == [],
          "a class with an empty label is the same as one with no label  "
          "<-- pinned defect")
    owner = {"labelExpressionInfo": {"expression": "$feature.OWNER"}}
    check(diff_drawing(
        drawing_key({"renderer": plain, "labelingInfo": [owner]}),
        drawing_key({"renderer": plain, "labelingInfo": [
            dict(owner, minScale=0, maxScale=0)]})) == [],
          "a label class with a minScale and a maxScale of 0 is the same as "
          "one that says nothing about its range  <-- pinned defect")

    # The same layer after a republish that changed nine things.
    drifted = json.loads(json.dumps(STYLED))
    drift_infos = drifted["renderer"]["uniqueValueInfos"]
    drift_infos[1]["symbol"]["color"] = [0, 112, 255, 255]     # 1. P recoloured
    del drift_infos[2]                                         # 2. X gone
    drift_infos.append({"value": "V", "label": "Void",
                        "symbol": sfs([0, 0, 0, 255])})        # 3. V new
    drift_infos[0]["label"] = "Current"                        # 4. A relabelled
    drifted["renderer"]["defaultSymbol"]["color"] = [200, 200, 200, 255]  # 5.
    drifted["renderer"]["defaultLabel"] = "Everything else"   # 6. legend text
    drifted["labelingInfo"][0]["minScale"] = 10000            # 7. label range
    drifted["transparency"] = 30                              # 8. transparency
    STYLED_SOURCE = {"name": "Parcels", "geometryType": "esriGeometryPolygon",
                     "fields": [{"name": "OBJECTID",
                                 "type": "esriFieldTypeOID"},
                                {"name": "STATUS", "type": "esriFieldTypeString",
                                 "length": 16}],
                     "minScale": 100000, "maxScale": 0,
                     "drawingInfo": STYLED}
    STYLED_SERVICE = dict(STYLED_SOURCE, minScale=50000,       # 9. range
                          drawingInfo=drifted)
    STYLED_AGAIN = dict(STYLED_SOURCE, drawingInfo=republished)
    styled_source = normalize_layer(STYLED_SOURCE, "styled")
    styled_service = normalize_layer(STYLED_SERVICE, "drifted")
    restyled = diff_layers(styled_source, styled_service)
    check(len(restyled) == 9,
          "the two styled fixtures differ in exactly nine ways and all nine "
          "are found")
    check(kinds(restyled) == ["CLASS_ADDED", "CLASS_LABEL_CHANGED",
                              "CLASS_REMOVED", "CLASS_SYMBOL_CHANGED",
                              "DEFAULT_SYMBOL_CHANGED", "DRAWING_INFO_CHANGED",
                              "LABEL_CLASS_CHANGED", "RENDERER_CHANGED",
                              "VISIBILITY_NARROWED"],
          "and they are the nine that were seeded, with no tenth")
    check(counts(restyled) == (3, 6),
          "three breaks and six warnings")
    check(one(restyled, "CLASS_SYMBOL_CHANGED", "renderer").severity == BREAK,
          "a class drawn in another colour is a BREAK: the legend now says "
          "something false about every feature in it  <-- pinned defect")
    check("class P" in one(restyled, "CLASS_SYMBOL_CHANGED", "renderer").detail
          and "symbol.color" in one(restyled, "CLASS_SYMBOL_CHANGED",
                                    "renderer").detail
          and "[0, 112, 255, 255]" in one(restyled, "CLASS_SYMBOL_CHANGED",
                                          "renderer").detail,
          "naming the class, the path to what changed and the new colour")
    check(one(restyled, "CLASS_REMOVED", "renderer").severity == BREAK
          and "value X" in one(restyled, "CLASS_REMOVED", "renderer").detail,
          "a class that is gone is a BREAK, and names its value  "
          "<-- pinned defect")
    check(one(restyled, "CLASS_ADDED", "renderer").severity == WARNING,
          "a new class is a warning")
    check(one(restyled, "CLASS_LABEL_CHANGED", "renderer").severity == WARNING
          and "Current" in one(restyled, "CLASS_LABEL_CHANGED",
                               "renderer").detail,
          "a new legend label is a warning, with the new label in it")
    check(one(restyled, "DEFAULT_SYMBOL_CHANGED", "renderer").severity
          == WARNING, "a default symbol in another colour is a warning")
    check("defaultLabel" in one(restyled, "RENDERER_CHANGED",
                                "renderer").detail,
          "a renderer property with no rule of its own is still compared, as "
          "JSON  <-- pinned defect")
    check(one(restyled, "LABEL_CLASS_CHANGED", "labelingInfo").severity
          == WARNING, "a label class that changed is a warning")
    check("minScale" in one(restyled, "LABEL_CLASS_CHANGED",
                            "labelingInfo").detail,
          "naming what in it changed")
    check(one(restyled, "DRAWING_INFO_CHANGED", "drawingInfo").severity
          == WARNING, "a new transparency is a warning")
    check(one(restyled, "VISIBILITY_NARROWED", "scaleRange").severity == BREAK,
          "a layer that stops drawing at a scale where it drew is a BREAK  "
          "<-- pinned defect")
    check([d.severity for d in restyled][:3] == [BREAK, BREAK, BREAK],
          "and the three breaks are at the top of the report")
    check(diff_layers(styled_source, normalize_layer(STYLED_AGAIN)) == [],
          "a layer republished with nothing but noise in its drawingInfo "
          "reports nothing at all  <-- pinned defect")
    check(diff_layers(styled_source, styled_service, symbology=False) == [],
          "and with symbology off the nine differences are not compared, "
          "which is what --schema-only and version 1.0 do")
    swapped_style = diff_layers(styled_service, styled_source)
    check("value V " in one(swapped_style, "CLASS_REMOVED", "renderer").detail
          and "value X " in one(swapped_style, "CLASS_ADDED",
                                "renderer").detail,
          "swapped, the added class is the one removed and the other way "
          "round")
    check(one(swapped_style, "VISIBILITY_WIDENED", "scaleRange") is not None,
          "and the narrowed range is a widened one")

    # ---- symbology: what a feature means on the map
    def drawn(renderer, **over):
        spec = {"renderer": renderer}
        spec.update(over)
        return drawing_key(spec)

    unique = STYLED["renderer"]
    retyped = diff_drawing(drawn(unique), drawn({"type": "simple",
                                                 "symbol": sfs([1, 2, 3])}))
    check(kinds(retyped) == ["RENDERER_TYPE_CHANGED"],
          "a unique value renderer republished as a simple one is one "
          "difference, and the classes of two kinds of renderer are not "
          "compared class by class  <-- pinned defect")
    check(retyped[0].severity == BREAK, "and it is a break")
    check("uniqueValue" in retyped[0].detail and "simple" in retyped[0].detail,
          "naming both kinds")
    lost = diff_drawing(drawn(unique), drawing_key({}))
    check(kinds(lost) == ["RENDERER_TYPE_CHANGED"] and "none" in lost[0].detail,
          "a renderer that is gone altogether is a change of type to none")
    check(diff_drawing(drawing_key({}), drawing_key({})) == [],
          "and two sides with no renderer at all agree")
    refield = diff_drawing(drawn(unique),
                           drawn(dict(unique, field1="ZONING")))
    check(kinds(refield) == ["RENDERER_FIELD_CHANGED"],
          "a renderer that reads another field is one difference, however "
          "many classes happen to match  <-- pinned defect")
    check(refield[0].severity == BREAK
          and "STATUS" in refield[0].detail and "ZONING" in refield[0].detail,
          "and it is a break naming both fields")
    arcade = dict(unique, valueExpression="$feature.STATUS")
    rearcade = diff_drawing(drawn(arcade), drawn(
        dict(unique, valueExpression="Upper($feature.STATUS)")))
    check(kinds(rearcade) == ["RENDERER_FIELD_CHANGED"],
          "an arcade expression that changed is a change of what is read")
    check(diff_drawing(drawn(arcade), drawn(
        dict(unique, valueExpression="$feature.STATUS\r\n"))) == [],
          "and one that only gained a line ending is not")
    total = {"type": "classBreaks", "field": "ACRES",
             "normalizationType": "esriNormalizeByPercentOfTotal",
             "normalizationTotal": 1000,
             "classBreakInfos": [{"classMaxValue": 50,
                                  "symbol": sfs([1, 2, 3])}]}
    retotal = diff_drawing(drawn(total),
                           drawn(dict(total, normalizationTotal=2000)))
    check(kinds(retotal) == ["RENDERER_FIELD_CHANGED"]
          and retotal[0].severity == BREAK,
          "a percent-of-total renderer whose total changed is a BREAK, "
          "because every feature moves into another class  <-- pinned defect")
    check("normalizationTotal 1000" in retotal[0].detail
          and "normalizationTotal 2000" in retotal[0].detail,
          "and the report names both totals")
    check(diff_drawing(drawn(total), drawn(
        dict(total, normalizationTotal=1000.0000000000001))) == [],
          "while a total written back with float noise on it has not changed")
    by_field = dict(total, normalizationType="esriNormalizeByField",
                    normalizationField="AREA")
    two_field = {"type": "uniqueValue", "field1": "STATUS", "field2": "ZONE",
                 "uniqueValueInfos": [{"value": "A,R1",
                                       "symbol": sfs([1, 2, 3])}]}
    for before, after, what in (
            (two_field, dict(two_field, field2="LANDUSE"),
             "a two field renderer whose second field changed"),
            (total, dict(total, field="JUST_VALUE"),
             "a class breaks renderer that reads another field"),
            (by_field, dict(by_field, normalizationField="POP"),
             "a renderer normalised by another field"),
            (by_field, dict(by_field, normalizationType="esriNormalizeByLog"),
             "a renderer normalised another way")):
        reread = diff_drawing(drawn(before), drawn(after))
        check(kinds(reread) == ["RENDERER_FIELD_CHANGED"]
              and reread[0].severity == BREAK,
              "%s is RENDERER_FIELD_CHANGED, a BREAK, and not a warning that "
              "exits 0  <-- pinned defect" % what)
    check(diff_drawing(drawn({"type": "simple", "symbol": sfs([1, 2, 3], 0)}),
                       drawn({"type": "simple",
                              "symbol": sfs([1, 2, 3], 1e-9)})) == [],
          "an outline width of 0 written back as 1e-9 is not a new symbol")
    nan_width = {"type": "simple", "symbol": sfs([1, 2, 3], float("nan"))}
    check(diff_drawing(drawn(nan_width), drawn(nan_width)) == [],
          "a symbol with a NaN width is the same as itself  <-- pinned defect")
    simple = {"type": "simple", "symbol": sfs([56, 168, 0, 255])}
    resimple = diff_drawing(drawn(simple), drawn(
        dict(simple, symbol=sfs([255, 0, 0, 255]))))
    check(kinds(resimple) == ["SYMBOL_CHANGED"],
          "a simple renderer in a new colour is reported")
    check(resimple[0].severity == WARNING,
          "and it is a warning: one symbol for every feature encodes no value, "
          "so a new colour says nothing false  <-- pinned defect")
    nodefault = dict(unique)
    del nodefault["defaultSymbol"]
    check(kinds(diff_drawing(drawn(unique), drawn(nodefault)))
          == ["DEFAULT_SYMBOL_REMOVED"],
          "a lost default symbol is reported")
    check(diff_drawing(drawn(unique), drawn(nodefault))[0].severity == BREAK,
          "and it is a break: a feature that matches no class stops drawing")
    check(kinds(diff_drawing(drawn(nodefault), drawn(unique)))
          == ["DEFAULT_SYMBOL_ADDED"], "a new default symbol is reported")
    check(diff_drawing(drawn(nodefault), drawn(unique))[0].severity
          == WARNING, "and it is a warning")
    ramp = {"type": "simple", "symbol": sfs([1, 2, 3]),
            "visualVariables": [{"type": "sizeInfo", "field": "ACRES",
                                 "minDataValue": 0, "maxDataValue": 20,
                                 "minSize": 4, "maxSize": 20}]}
    reramp = json.loads(json.dumps(ramp))
    reramp["visualVariables"][0]["maxDataValue"] = 40
    everything = json.loads(json.dumps(unique))
    for info in everything["uniqueValueInfos"]:
        info["symbol"]["color"] = [0, 0, 0, 255]
        info["symbol"]["outline"]["width"] = 2
    repainted = diff_drawing(drawn(unique), drawn(everything))
    check(kinds(repainted) == ["CLASS_SYMBOL_CHANGED"] * 3,
          "a republish that repainted every class reports one line per class, "
          "not one line for the renderer")
    check(all("symbol.color" in d.detail and "width" not in d.detail
              for d in repainted),
          "and each line names the first path that differs, not every one")
    cim = {"type": "simple", "symbol": {
        "type": "CIMSymbolReference", "symbol": {
            "type": "CIMPolygonSymbol", "symbolLayers": [
                {"type": "CIMSolidFill", "enable": True,
                 "color": {"type": "CIMRGBColor",
                           "values": [56, 168, 0, 100]}}]}}}
    recim = json.loads(json.dumps(cim))
    recim["symbol"]["symbol"]["symbolLayers"][0]["color"]["values"][0] = 57
    check(diff_drawing(drawn(cim), drawn(recim))[0].detail.startswith(
        "symbol.symbol.symbolLayers[0].color.values[0] is 56"),
          "a CIM symbol is compared as JSON, down to the value that changed")
    check(first_mismatch({"a": 1}, {"a": 1, "b": 2}) == ("b", None, 2)
          and first_mismatch({"a": 1, "b": 2}, {"a": 1}) == ("b", 2, None),
          "a key on one side only is a mismatch, whichever side has it  "
          "<-- pinned defect")
    bare = {"type": "esriSFS", "style": "esriSFSSolid", "color": [1, 2, 3, 255]}
    outlined = dict(bare, outline={"type": "esriSLS", "style": "esriSLSSolid",
                                   "color": [255, 0, 0, 255], "width": 4})
    bare_classes = {"type": "uniqueValue", "field1": "STATUS",
                    "uniqueValueInfos": [{"value": "A", "symbol": bare}]}
    outlined_classes = dict(bare_classes, uniqueValueInfos=[
        {"value": "A", "symbol": outlined}])
    gained = diff_drawing(drawn(bare_classes), drawn(outlined_classes))
    check(kinds(gained) == ["CLASS_SYMBOL_CHANGED"]
          and gained[0].severity == BREAK
          and "symbol.outline" in gained[0].detail,
          "a class symbol that gains a red outline only in the service is a "
          "BREAK, although the source has no outline key to walk  "
          "<-- pinned defect")
    shed = diff_drawing(drawn(outlined_classes), drawn(bare_classes))
    check(kinds(shed) == ["CLASS_SYMBOL_CHANGED"] and shed[0].severity == BREAK,
          "and one that loses its outline in the service is the same break")
    three_stops = [{"value": 0, "color": [0, 0, 0, 255]},
                   {"value": 50, "color": [9, 9, 9, 255]},
                   {"value": 100, "color": [255, 255, 255, 255]}]

    def stopped(stops):
        return drawn({"type": "simple", "symbol": bare, "visualVariables": [
            {"type": "colorInfo", "field": "V", "stops": stops}]})

    for longer, shorter, what in ((three_stops, three_stops[:2], "lost"),
                                  (three_stops[:2], three_stops, "gained")):
        cut = diff_drawing(stopped(longer), stopped(shorter))
        check(kinds(cut) == ["COLOUR_RAMP_CHANGED"]
              and cut[0].severity == BREAK and "stops" in cut[0].detail,
              "a colour ramp that %s its last stop, one list a prefix of the "
              "other, is a break and not an IndexError  <-- pinned defect"
              % what)
    recoloured = json.loads(json.dumps(three_stops))
    recoloured[2]["color"] = [0, 0, 255, 255]
    hue = diff_drawing(stopped(three_stops), stopped(recoloured))
    check(kinds(hue) == ["COLOUR_RAMP_CHANGED"]
          and hue[0].severity == BREAK
          and "stops[2].color is [255, 255, 255, 255]" in hue[0].detail,
          "a colour ramp whose top stop is now blue is a break: every feature "
          "near the top is drawn in a colour the legend gave another value  "
          "<-- pinned defect")
    relabelled = json.loads(json.dumps(three_stops))
    relabelled[2]["label"] = "> 100"
    legend = diff_drawing(stopped(three_stops), stopped(relabelled))
    check(kinds(legend) == ["VISUAL_VARIABLES_CHANGED"]
          and legend[0].severity == WARNING,
          "while a new label on a stop is legend text, and a warning")
    both = diff_drawing(stopped(three_stops), drawn(
        {"type": "simple", "symbol": bare, "visualVariables": [
            {"type": "colorInfo", "field": "W", "stops": recoloured}]}))
    check(sorted(kinds(both)) == ["COLOUR_RAMP_CHANGED",
                                  "VARIABLE_FIELD_CHANGED"],
          "a ramp moved to another field and another colour says so twice")

    def listed(colours, top=100):
        return drawn({"type": "simple", "symbol": bare, "visualVariables": [
            {"type": "colorInfo", "field": "V", "minDataValue": 0,
             "maxDataValue": top, "colors": colours}]})

    check(diff_drawing(listed([[255, 0, 0, 255], [0, 0, 255, 255]]),
                       listed([[255, 0, 0], [0, 0, 254.9999]])) == [],
          "a ramp written as a colors list that lost its alpha and picked up "
          "float noise is the same ramp  <-- pinned defect")
    swapped = diff_drawing(listed([[255, 0, 0, 255], [0, 0, 255, 255]]),
                           listed([[255, 0, 0, 255], [0, 255, 0, 255]]))
    check(kinds(swapped) == ["COLOUR_RAMP_CHANGED"]
          and "colors[1] is [0, 0, 255, 255]" in swapped[0].detail,
          "while one colour of it that changed is a break, named as the "
          "whole colour and not one channel")
    stretched = diff_drawing(listed([[255, 0, 0, 255], [0, 0, 255, 255]]),
                             listed([[255, 0, 0, 255], [0, 0, 255, 255]], 200))
    check(kinds(stretched) == ["COLOUR_RAMP_CHANGED"]
          and stretched[0].severity == BREAK,
          "and so is a colour ramp stretched over another data range: a value "
          "of 100 was the top colour and is now halfway")
    check(kinds(diff_drawing(drawn(ramp), drawn(reramp)))
          == ["VISUAL_VARIABLES_CHANGED"],
          "a visual variable that changed is reported")
    check("maxDataValue" in diff_drawing(drawn(ramp), drawn(reramp))[0].detail,
          "naming what in it changed")
    check(diff_drawing(drawn(ramp), drawn(reramp))[0].severity == WARNING,
          "and it is a warning: a colour or size ramp moved no feature into "
          "another class  <-- pinned defect")
    shaded = {"type": "simple", "symbol": sfs([1, 2, 3]), "visualVariables": [
        {"type": "colorInfo", "field": "POP",
         "stops": [{"value": 0, "color": [0, 0, 0, 255]},
                   {"value": 100, "color": [255, 255, 255, 255]}]},
        {"type": "sizeInfo", "field": "ACRES", "minSize": 4, "maxSize": 40}]}
    reshaded = json.loads(json.dumps(shaded))
    reshaded["visualVariables"].reverse()
    reshaded["visualVariables"][1]["stops"].reverse()
    check(diff_drawing(drawn(shaded), drawn(reshaded)) == [],
          "a colorInfo and a sizeInfo listed in the other order, with the "
          "colour stops reversed too, draw the same map and are not drift  "
          "<-- pinned defect")
    refield_ramp = json.loads(json.dumps(shaded))
    refield_ramp["visualVariables"][0]["field"] = "DENSITY"
    moved_ramp = diff_drawing(drawn(shaded), drawn(refield_ramp))
    check(kinds(moved_ramp) == ["VARIABLE_FIELD_CHANGED"]
          and moved_ramp[0].severity == BREAK
          and "visual variable colorInfo: it reads field POP in the source "
              "and field DENSITY" in moved_ramp[0].detail,
          "a visual variable is matched by its type, so a colour ramp moved "
          "to another field is a break named as that and not as a position "
          "in a list  <-- pinned defect")
    resized = json.loads(json.dumps(shaded))
    resized["visualVariables"][1]["field"] = "PERIMETER"
    check(kinds(diff_drawing(drawn(shaded), drawn(resized)))
          == ["VARIABLE_FIELD_CHANGED"],
          "and a size ramp moved to another field is the same break")
    for change, what in (({"valueExpression": "$feature.POP / 2"},
                          "an arcade expression"),
                         ({"normalizationField": "AREA"}, "a normalization")):
        rescaled = json.loads(json.dumps(shaded))
        rescaled["visualVariables"][0].update(change)
        check(kinds(diff_drawing(drawn(shaded), drawn(rescaled)))
              == ["VARIABLE_FIELD_CHANGED"],
              "and so is a colour ramp that gained %s" % what)
    unramped = diff_drawing(drawn(shaded), drawn(dict(
        shaded, visualVariables=shaded["visualVariables"][:1])))
    check(kinds(unramped) == ["VISUAL_VARIABLE_REMOVED"]
          and unramped[0].severity == BREAK
          and "source has the visual variable sizeInfo" in unramped[0].detail,
          "a visual variable that is gone is a break naming its type: the "
          "value it showed is gone from the map  <-- pinned defect")
    ramped = diff_drawing(drawn(simple), drawn(dict(simple, visualVariables=[
        {"type": "sizeInfo", "target": "outline", "minSize": 1}])))
    check(kinds(ramped) == ["VISUAL_VARIABLE_ADDED"]
          and "service has the visual variable sizeInfo on outline"
          in ramped[0].detail,
          "and a new one is named with its target")
    check(ramped[0].severity == WARNING,
          "and a new visual variable is a warning: nothing that drew "
          "yesterday draws differently  <-- pinned defect")
    dots = {"type": "dotDensity", "dotValue": 100, "attributes": [
        {"field": "POP", "color": [255, 0, 0, 255], "label": "People"},
        {"field": "JOBS", "color": [0, 0, 255, 255], "label": "Jobs"}]}

    def dotted(**change):
        out = json.loads(json.dumps(dots))
        out["attributes"][0].update(change)
        return drawn(out)

    counted = diff_drawing(drawn(dots), dotted(field="HOUSING"))
    check(kinds(counted) == ["RENDERER_FIELD_CHANGED"]
          and counted[0].severity == BREAK
          and "attribute 0: it reads field POP" in counted[0].detail,
          "a dot density attribute moved to another field is a break, not "
          "plain json that exits 0  <-- pinned defect")
    check(kinds(diff_drawing(drawn(dots), dotted(color=[0, 255, 0])))
          == ["CLASS_SYMBOL_CHANGED"],
          "an attribute drawn in another colour is a class symbol that changed")
    check(diff_drawing(drawn(dots), dotted(color=[255, 0, 0])) == [],
          "while one that only lost its alpha has not")
    check(kinds(diff_drawing(drawn(dots), dotted(label="Residents")))
          == ["CLASS_LABEL_CHANGED"], "a new legend label is a label change")
    lone = dict(dots, attributes=dots["attributes"][:1])
    shrunk = diff_drawing(drawn(dots), drawn(lone))
    check(kinds(shrunk) == ["CLASS_REMOVED"]
          and "attribute 1" in shrunk[0].detail,
          "an attribute that is gone is a class that is gone")
    check(shrunk[0].severity == BREAK,
          "and it is a BREAK, so a dot density map that lost a counted field "
          "fails the run rather than exiting 0  <-- pinned defect")
    grown = diff_drawing(drawn(lone), drawn(dots))
    check(kinds(grown) == ["CLASS_ADDED"],
          "and one that is new is a class that is new")
    check(grown[0].severity == WARNING,
          "which is a warning, as a new unique value class is  "
          "<-- pinned defect")
    turned = dict(dots, attributes=dots["attributes"][::-1])
    check(sorted(kinds(diff_drawing(drawn(dots), drawn(turned))))
          == ["CLASS_SYMBOL_CHANGED"] * 2 + ["RENDERER_FIELD_CHANGED"] * 2,
          "attributes are paired by position, so two that swapped places are "
          "two changed fields and two changed colours, which they are on a "
          "pie chart")
    raises(lambda: drawn(dict(dots, attributes={"field": "POP"})),
           "attributes that is an object raises")
    raises(lambda: drawn(dict(dots, attributes=["POP"])),
           "and so does an attribute that is a bare string")
    check(variable_name(("", "")) == "(no type)",
          "a visual variable with no type is still named")
    raises(lambda: drawn(dict(simple, visualVariables={"type": "colorInfo"})),
           "visualVariables that is an object raises")
    raises(lambda: drawn(dict(simple, visualVariables=["colorInfo"])),
           "and so does a visual variable that is a bare string")

    # ---- symbology: unique value classes
    pair = {"type": "uniqueValue", "field1": "STATUS", "field2": "ZONE",
            "fieldDelimiter": ", ",
            "uniqueValueInfos": [{"value": "A, R1", "symbol": sfs([1, 2, 3])},
                                 {"value": "A, C2", "symbol": sfs([4, 5, 6])}]}
    check(diff_drawing(drawn(pair), drawn(dict(pair, fieldDelimiter=",",
        uniqueValueInfos=[{"value": "A,C2", "symbol": sfs([4, 5, 6])},
                          {"value": "A,R1", "symbol": sfs([1, 2, 3])}])))
          == [],
          "two fields joined by \", \" on one side and \",\" on the other are "
          "the same classes  <-- pinned defect")
    check(sorted(unique_classes(drawn(pair)["renderer"]))
          == [("A", "C2"), ("A", "R1")],
          "a two field value is read as its two parts")
    check(sorted(unique_classes({"field1": "A", "field3": "C",
                                 "uniqueValueInfos": [{"value": "x,z"}]}))
          == [("x", "z")],
          "and so is a value that names a field3, even with no field2")
    tight = dict(pair, fieldDelimiter=",")
    check(diff_drawing(drawn(tight), drawn(dict(tight, uniqueValueInfos=[
        {"value": "A,R1", "symbol": sfs([1, 2, 3])},
        {"value": "A,C2", "symbol": sfs([4, 5, 6])}]))) == [],
          "and with \",\" declared on both sides, \"A, R1\" and \"A,R1\" are "
          "still one class, because each part is trimmed  <-- pinned defect")
    undelimited = dict(pair)
    del undelimited["fieldDelimiter"]
    check(diff_drawing(drawn(pair), drawn(dict(undelimited, uniqueValueInfos=[
        {"value": "A,R1", "symbol": sfs([1, 2, 3])},
        {"value": "A,C2", "symbol": sfs([4, 5, 6])}]))) == [],
          "a two field renderer that does not name its delimiter is read "
          "with a comma, which is the default")
    raises(lambda: drawn(dict(pair, fieldDelimiter=5)),
           "a delimiter that is a number raises a ValueError, not the "
           "TypeError str.split would  <-- pinned defect")
    check(unique_classes(dict(unique, fieldDelimiter=5)) != {},
          "and a single field renderer never splits, so its delimiter is "
          "never read")
    numeric = {"type": "uniqueValue", "field1": "CODE",
               "uniqueValueInfos": [{"value": 1, "symbol": sfs([1, 2, 3])},
                                    {"value": 2.0, "symbol": sfs([4, 5, 6])}]}
    check(diff_drawing(drawn(numeric), drawn(dict(numeric, uniqueValueInfos=[
        {"value": "1", "symbol": sfs([1, 2, 3])},
        {"value": "2", "symbol": sfs([4, 5, 6])}]))) == [],
          "a class for the number 1 and a class for the text \"1\" are one "
          "class  <-- pinned defect")
    grouped = {"type": "uniqueValue", "field1": "CODE",
               "uniqueValueGroups": [{"heading": "Codes", "classes": [
                   {"values": [[1]], "symbol": sfs([1, 2, 3])},
                   {"values": [[2]], "symbol": sfs([4, 5, 6])}]}]}
    check(diff_drawing(drawn(numeric), drawn(grouped)) == [],
          "a side that lists its classes only as uniqueValueGroups is read "
          "from them, and agrees with the same classes as uniqueValueInfos")
    check(diff_drawing(drawn(numeric), drawn(dict(grouped, uniqueValueGroups=[
        {"classes": [{"values": [[1.0]], "symbol": sfs([1, 2, 3])},
                     {"values": [["2"]], "symbol": sfs([4, 5, 6])}]}]))) == [],
          "and a group that writes 1 as 1.0 or 2 as \"2\" still agrees, "
          "because a group value is spelled the same way as any other  "
          "<-- pinned defect")
    fraction = {"type": "uniqueValue", "field1": "RATE",
                "uniqueValueInfos": [{"value": 0.3, "symbol": sfs([1, 2, 3])}]}
    check(diff_drawing(drawn(fraction), drawn(dict(fraction, uniqueValueInfos=[
        {"value": 0.30000000000000004, "symbol": sfs([1, 2, 3])}]))) == [],
          "a class for 0.3 written back as 0.30000000000000004 is the same "
          "class, not one removed and one added  <-- pinned defect")
    check(sorted(unique_classes({"uniqueValueGroups": [{"classes": [
        {"values": [["A", "R1"], ["B", "R1"]]}]}]}))
          == [("A", "R1"), ("B", "R1")],
          "and one group class that draws two values is two classes")
    check(unique_classes({}) == {},
          "a renderer with no classes at all has none")
    raises(lambda: drawn(dict(numeric, uniqueValueInfos=[
        {"value": 1}, {"value": "1"}])),
           "a renderer with two classes for one value raises, because no "
           "comparison of it can be trusted  <-- pinned defect")
    raises(lambda: drawn(dict(numeric, uniqueValueInfos={"value": 1})),
           "uniqueValueInfos that is an object raises")
    raises(lambda: drawn(dict(numeric, uniqueValueInfos=["A"])),
           "a class that is a bare string raises")
    raises(lambda: drawn({"type": "uniqueValue",
                          "uniqueValueGroups": ["Codes"]}),
           "a group that is a bare string raises")
    raises(lambda: drawn({"type": "uniqueValue", "uniqueValueGroups": [
        {"classes": [{"values": ["A"]}]}]}),
           "and a group class whose values are not lists raises")

    # ---- symbology: class breaks
    BREAKS = {"type": "classBreaks", "field": "ACRES", "minValue": 0,
              "classBreakInfos": [
                  {"classMaxValue": 1, "label": "0 - 1",
                   "symbol": sfs([255, 255, 204, 255])},
                  {"classMaxValue": 5, "label": "1 - 5",
                   "symbol": sfs([161, 218, 180, 255])},
                  {"classMaxValue": 20, "label": "5 - 20",
                   "symbol": sfs([44, 127, 184, 255])}]}
    noisy = json.loads(json.dumps(BREAKS))
    noisy["classBreakInfos"].reverse()
    noisy["classBreakInfos"][1]["classMaxValue"] = 5.0000000001
    noisy["classBreakInfos"][1]["classMinValue"] = 1
    noisy["classBreakInfos"][0]["classMinValue"] = 5.0
    check(diff_drawing(drawn(BREAKS), drawn(noisy)) == [],
          "class breaks listed in another order, with float noise on one and "
          "a classMinValue the server wrote out, are the same breaks  "
          "<-- pinned defect")
    moved = json.loads(json.dumps(BREAKS))
    moved["classBreakInfos"][1]["classMaxValue"] = 6
    rebroken = diff_drawing(drawn(BREAKS), drawn(moved))
    check(kinds(rebroken) == ["CLASS_BREAKS_CHANGED"],
          "a break that moved from 5 to 6 is one difference, and the symbols "
          "are not then compared against a different range  "
          "<-- pinned defect")
    check(rebroken[0].severity == BREAK, "and it is a break")
    check("0, 1, 5, 20" in rebroken[0].detail
          and "0, 1, 6, 20" in rebroken[0].detail,
          "listing the breaks on both sides")
    raised = json.loads(json.dumps(BREAKS))
    raised["classBreakInfos"][2]["classMaxValue"] = 30
    check(kinds(diff_drawing(drawn(BREAKS), drawn(raised)))
          == ["CLASS_BREAKS_CHANGED"],
          "the top of the last class moving from 20 to 30 is a change, and "
          "no class above it carries that bound down  <-- pinned defect")
    raises(lambda: drawn(dict(BREAKS, classBreakInfos=[
        {"classMaxValue": float("nan"), "symbol": sfs([1, 2, 3])}])),
           "a class break whose top is NaN raises, because it has no place "
           "in the sort and would pair with another class by chance  "
           "<-- pinned defect")
    fewer = dict(BREAKS, classBreakInfos=BREAKS["classBreakInfos"][:2])
    check(kinds(diff_drawing(drawn(BREAKS), drawn(fewer)))
          == ["CLASS_BREAKS_CHANGED"], "a class fewer is a change of breaks")
    check(kinds(diff_drawing(drawn(BREAKS), drawn(dict(BREAKS, minValue=0.5))))
          == ["CLASS_BREAKS_CHANGED"],
          "and so is a first class that starts somewhere else")
    unbounded = dict(BREAKS)
    del unbounded["minValue"]
    check(diff_drawing(drawn(BREAKS), drawn(unbounded)) == [],
          "a side that does not say where the first class starts is not a "
          "side that moved it")
    gapped = json.loads(json.dumps(BREAKS))
    gapped["classBreakInfos"][1]["classMinValue"] = 2
    check(kinds(diff_drawing(drawn(BREAKS), drawn(gapped)))
          == ["CLASS_BREAKS_CHANGED"],
          "a class whose own classMinValue opens a gap below it is a change")
    recolour = json.loads(json.dumps(BREAKS))
    recolour["classBreakInfos"][1]["symbol"]["color"] = [255, 0, 0, 255]
    recoloured = diff_drawing(drawn(BREAKS), drawn(recolour))
    check(kinds(recoloured) == ["CLASS_SYMBOL_CHANGED"],
          "a class drawn in another colour on the same breaks is reported")
    check("class 1 to 5" in recoloured[0].detail,
          "naming the class by its range")
    two_breaks = {"type": "classBreaks", "field": "ACRES", "minValue": 0,
                  "classBreakInfos": [{"classMaxValue": 10},
                                      {"classMaxValue": 20}]}
    lifted = json.loads(json.dumps(two_breaks))
    lifted["classBreakInfos"][1]["classMinValue"] = 15
    lifted_diff = diff_drawing(drawn(two_breaks), drawn(lifted))
    check(kinds(lifted_diff) == ["CLASS_BREAKS_CHANGED"]
          and "breaks 0, 10, 20 in the source, 0 to 10, 15 to 20 in the "
              "service" in lifted_diff[0].detail,
          "a second class that now starts at 15 is named by its range, so "
          "the two sides of the line do not read the same  <-- pinned defect")
    big = {"type": "classBreaks", "field": "VALUE", "minValue": 0,
           "classBreakInfos": [{"classMaxValue": 1000000},
                               {"classMaxValue": 5000000}]}
    nudged = json.loads(json.dumps(big))
    nudged["classBreakInfos"][0]["classMaxValue"] = 1000001
    check(kinds(diff_drawing(drawn(big), drawn(nudged)))
          == ["CLASS_BREAKS_CHANGED"],
          "a break edited from 1000000 to 1000001 is a change: a feature "
          "valued 1000001 moves class  <-- pinned defect")
    huge = json.loads(json.dumps(big))
    huge["classBreakInfos"][1]["classMaxValue"] = 10000000000
    edited = json.loads(json.dumps(huge))
    edited["classBreakInfos"][1]["classMaxValue"] = 10000000009
    check(kinds(diff_drawing(drawn(huge), drawn(edited)))
          == ["CLASS_BREAKS_CHANGED"],
          "and so is a break edited from 10000000000 to 10000000009, which a "
          "relative tolerance of 1e-9 read as noise and passed  "
          "<-- pinned defect")
    floated = json.loads(json.dumps(big))
    floated["classBreakInfos"][0]["classMaxValue"] = 1000000.0
    check(kinds(diff_drawing(drawn(floated), drawn(nudged)))
          == ["CLASS_BREAKS_CHANGED"],
          "and so is the same edit from a break the server wrote as "
          "1000000.0  <-- pinned defect")
    check(breaks_text([]) == "no classes", "no classes describes as such")
    check(bounds_name((None, 5)) == "up to 5",
          "a class with no known lower bound is named by its top")
    check(breaks_text(break_classes(unbounded)) == "1, 5, 20",
          "and a list of breaks with no known start begins at the first top")
    raises(lambda: drawn({"type": "classBreaks",
                          "classBreakInfos": [{"label": "x"}]}),
           "a class break with no classMaxValue raises")
    raises(lambda: drawn({"type": "classBreaks", "classBreakInfos": "x"}),
           "classBreakInfos that is a string raises")
    raises(lambda: drawn(["simple"]), "a renderer that is a list raises")
    raises(lambda: drawing_key([]), "a drawingInfo that is a list raises")

    # ---- symbology: labels
    LABELS = STYLED["labelingInfo"]
    unlabelled = diff_drawing(drawn(simple, labelingInfo=LABELS), drawn(simple))
    check(kinds(unlabelled) == ["LABEL_CLASS_REMOVED"],
          "a label class that is gone is reported")
    check(unlabelled[0].severity == WARNING,
          "and it is a warning: no feature moves")
    check("$feature.OWNER" in unlabelled[0].detail,
          "naming what it labelled")
    check(kinds(diff_drawing(drawn(simple), drawn(simple,
                                                  labelingInfo=LABELS)))
          == ["LABEL_CLASS_ADDED"], "a new label class is reported")
    check(diff_drawing(drawn(simple), drawn(simple, labelingInfo=LABELS))[0]
          .severity == WARNING,
          "and it is a warning too  <-- pinned defect")
    rewhere = diff_drawing(
        drawn(simple, labelingInfo=[dict(LABELS[0], where="ACRES > 1")]),
        drawn(simple, labelingInfo=[dict(LABELS[0], where="ACRES > 2")]))
    check(kinds(rewhere) == ["LABEL_CLASS_ADDED", "LABEL_CLASS_REMOVED"],
          "two label classes that differ only by their where clause label "
          "different features, so they are one added and one removed, not "
          "one changed  <-- pinned defect")
    split = [dict(LABELS[0], where="STATUS = 'A'"),
             dict(LABELS[0], where="STATUS = 'P'", minScale=2000)]
    check(diff_drawing(drawn(simple, labelingInfo=split),
                       drawn(simple, labelingInfo=list(reversed(split)))) == [],
          "two label classes for one expression, told apart by their where "
          "clause, match whatever order they are listed in  "
          "<-- pinned defect")
    legacy = [{"labelExpression": "[OWNER]", "minScale": 5000}]
    check(label_classes(canonical_drawing(legacy))
          == {("[OWNER]", ""): [{"labelExpression": "[OWNER]",
                                 "minScale": 5000}]},
          "a label class with only the older labelExpression is keyed on it")
    twice = [dict(LABELS[0]), dict(LABELS[0], minScale=9000)]
    check(diff_drawing(drawn(simple, labelingInfo=twice),
                       drawn(simple, labelingInfo=list(reversed(twice))))
          == [],
          "two label classes with one expression and one where clause match "
          "whatever order they are listed in  <-- pinned defect")
    check(kinds(diff_drawing(drawn(simple, labelingInfo=twice),
                             drawn(simple, labelingInfo=twice[:1])))
          == ["LABEL_CLASS_REMOVED"],
          "of two label classes with one expression, the one left over is "
          "the one reported")
    crossed = diff_drawing(
        drawn(simple, labelingInfo=twice),
        drawn(simple, labelingInfo=[dict(LABELS[0], minScale=9500),
                                    dict(LABELS[0], minScale=1000)]))
    check(kinds(crossed) == ["LABEL_CLASS_CHANGED", "LABEL_CLASS_CHANGED"],
          "two label classes with one expression that both changed are two "
          "changes, whichever way round they were paired")
    check(kinds(diff_drawing(
        drawn(simple, labelingInfo=[{"labelExpression": "[OWNER]"}]),
        drawn(simple, labelingInfo=[{"labelExpressionInfo": {
            "expression": "$feature.OWNER"}}])))
          == ["LABEL_CLASS_ADDED", "LABEL_CLASS_REMOVED"],
          "the older labelExpression and the same label in arcade are not "
          "translated, and read as one class removed and one added")
    check(label_name(("", "")) == "(no expression)",
          "a label class with no expression is still named")
    check(label_name(("$feature.OWNER", "A = 1"))
          == "$feature.OWNER where A = 1", "and a where clause is named too")
    raises(lambda: drawn(simple, labelingInfo={"a": 1}),
           "labelingInfo that is an object raises")
    raises(lambda: drawn(simple, labelingInfo=["[OWNER]"]),
           "a label class that is a bare string raises")

    # ---- symbology: the visibility range
    check(scale_range({}) is None, "a layer with no scale range says so")
    check(scale_range({"minScale": 0, "maxScale": 0}) == (0, None),
          "a range of 0 and 0 is visible at every scale")
    check(scale_range({"minScale": None, "maxScale": None}) is None,
          "and a range of two nulls is no range reported")
    check(scale_range({"minScale": 100000}) == (0, 100000),
          "a missing maxScale is no inner limit")
    raises(lambda: scale_range({"minScale": "100000"}),
           "a scale that is a string raises")
    raises(lambda: scale_range({"minScale": -1}), "a negative scale raises")
    raises(lambda: scale_range({"maxScale": True}),
           "a scale of true raises  <-- pinned defect")
    check(diff_scale_range((0, None), (0, None)) == [],
          "the same range is no difference")
    check(diff_scale_range((0, 100000), (0, 100000.00000000001)) == [],
          "and float noise on a scale is no difference")
    check(kinds(diff_scale_range((0, None), (0, 100000)))
          == ["VISIBILITY_NARROWED"],
          "a layer visible at every scale that gains an outer limit narrowed  "
          "<-- pinned defect")
    check(kinds(diff_scale_range((0, 100000), (0, None)))
          == ["VISIBILITY_WIDENED"], "and losing the limit widened it")
    widened = diff_scale_range((0, 50000), (0, 100000))
    check(kinds(widened) == ["VISIBILITY_WIDENED"]
          and widened[0].severity == WARNING,
          "a range widened from 1:50000 to 1:100000 is a warning, not a "
          "break that fails the nightly run  <-- pinned defect")
    check(kinds(diff_scale_range((0, 1000000), (0, 1000001)))
          == ["VISIBILITY_WIDENED"],
          "a scale edited from 1:1000000 to 1:1000001 is a change, because "
          "the tolerance holds nothing a person could type  <-- pinned defect")
    check(kinds(diff_scale_range((1000, None), (2000, None)))
          == ["VISIBILITY_NARROWED"],
          "an inner limit that moved out narrows the range")
    check(kinds(diff_scale_range((2000, None), (1000, None)))
          == ["VISIBILITY_WIDENED"], "and one that moved in widens it")
    check(kinds(diff_scale_range((1000, 100000), (500, 50000)))
          == ["VISIBILITY_NARROWED"],
          "a range that widened at one end and narrowed at the other narrowed, "
          "because it stops drawing somewhere it drew  <-- pinned defect")
    check(describe_scales((0, None)) == "at every scale",
          "an unlimited range describes as every scale")
    check(describe_scales((1000, None))
          == "from 1:1000 out to the widest zoom", "an inner limit describes")
    check(describe_scales((0, 100000))
          == "from the closest zoom out to 1:100000", "an outer one too")
    check(scales_same(None, None) and not scales_same(None, 5),
          "no limit is only the same as no limit")

    # ---- symbology: a side that does not say
    no_drawing = normalize_layer({"fields": [], "drawingInfo": None})
    check("drawingInfo" not in no_drawing and "scaleRange" not in no_drawing,
          "a layer with a null drawingInfo and no scales has neither")
    check(diff_layers(normalize_layer(dict(STYLED_SOURCE, drawingInfo=None,
                                           minScale=None, maxScale=None)),
                      styled_service) == [],
          "so a side with no drawingInfo is never reported as having lost "
          "its renderer, which is what an arcpy-read feature class looks "
          "like  <-- pinned defect")
    raises(lambda: normalize_layer({"drawingInfo": "renderer"}),
           "a drawingInfo that is a string raises when the layer is "
           "normalized")
    check(normalize_layer({"drawingInfo": "renderer", "minScale": -1},
                          symbology=False) == {"label": "", "name": "",
                                               "fields": []},
          "but with symbology off it is never read, so a renderer this file "
          "refuses cannot stop a --schema-only run  <-- pinned defect")

    # ---- nothing leaks the token
    TOKEN = "SELFTEST-TOKEN-0000000000"
    check(redact("read %s ok" % TOKEN, TOKEN) == "read %s ok" % REDACTED,
          "a token given to redact is taken out of the text")
    check(TOKEN not in redact("https://gis/x/0?f=json&token=%s" % TOKEN),
          "a token in a url query string is taken out even when redact was "
          "not told what the token is  <-- pinned defect")
    check(redact("?token=abc&f=json") == "?token=%s&f=json" % REDACTED,
          "and the parameter after it survives, so the message stays readable")
    check(redact("?TOKEN=abc") == "?TOKEN=%s" % REDACTED,
          "a token parameter in another case is redacted too")
    check(redact("") == "", "empty text redacts to empty")
    check(redact("no secret here") == "no secret here",
          "text with no token in it is left alone, because a message with "
          "nothing in it is not a message")
    check(redact(ValueError("token=abc")) == "token=%s" % REDACTED,
          "an exception object is redacted, which is how urllib's own message "
          "is handled  <-- pinned defect")

    # ---- urls
    check(is_http_url("https://gis/x/0") is True, "an https url is one")
    check(is_http_url("http://gis/x/0") is True, "an http url is one")
    check(is_http_url("  https://gis/x/0 ") is True,
          "with spaces around it, it is still one")
    check(is_http_url("C:/data/parcels.gdb/Parcels") is False,
          "a windows path is not a url  <-- pinned defect")
    check(is_http_url("/data/parcels.gdb/Parcels") is False,
          "and neither is a posix path  <-- pinned defect")
    check(is_http_url("ftp://gis/x") is False, "and neither is ftp")
    check(is_http_url(None) is False, "and neither is nothing at all")
    check(source_kind("https://gis/x/0") == "service",
          "a url names another service")
    check(source_kind("baseline.json") == "snapshot", "a .json names a snapshot")
    check(source_kind("BASELINE.JSON") == "snapshot",
          "in any case  <-- pinned defect")
    check(source_kind("C:/data/parcels.gdb/Parcels") == "dataset",
          "a geodatabase path names a dataset")
    check(source_kind("/home/gis/parcels.sde/schema.Parcels") == "dataset",
          "and so does an sde path")
    check(source_kind("https://gis/x/0.json") == "service",
          "a url that happens to end in .json is still a service  "
          "<-- pinned defect")
    raises(lambda: source_kind(""), "an empty --source raises")
    raises(lambda: source_kind(None), "a null --source raises")
    check(build_url("https://gis/x/0", {}) == "https://gis/x/0?f=json",
          "f=json is put on a url that has no query string")
    check(build_url("https://gis/x/0?f=html", {}) == "https://gis/x/0?f=json",
          "and an f=html pasted out of the rest browser is removed, rather "
          "than returning a web page to json.loads  <-- pinned defect")
    check(build_url("https://gis/x/0/", {}) == "https://gis/x/0?f=json",
          "a trailing slash is dropped")
    check(build_url("https://gis/x/0#top", {}) == "https://gis/x/0?f=json",
          "and so is a fragment")
    check(build_url("https://gis/x/0", {"token": "a b"})
          == "https://gis/x/0?f=json&token=a+b",
          "a parameter is quoted rather than pasted in raw")
    check(build_url("https://gis/x/0", {"b": "2", "a": "1"})
          == build_url("https://gis/x/0", {"a": "1", "b": "2"}),
          "the parameters come out in a fixed order, so two runs build the "
          "same url  <-- pinned defect")
    raises(lambda: build_url("C:/data/x.gdb/Parcels", {}),
           "building a url out of a file path raises")
    check(clean_url("https://gis/x/0?f=html") + "/query"
          == "https://gis/x/0/query",
          "an operation appended to a pasted url lands on the layer and not "
          "inside its query string, which is how a probe ends up asking for "
          "the layer definition and failing on a features list  "
          "<-- pinned defect")
    check(clean_url("https://gis/x/0/#top") == "https://gis/x/0",
          "the trailing slash and the fragment go the same way")
    raises(lambda: clean_url("C:/data/x.gdb/Parcels"),
           "and cleaning a file path raises rather than returning one")
    check(same_host("https://GIS:443/a/0", " https://gis:443/b/1"),
          "two layers on one host and port are the same host, in any case")
    check(not same_host("https://gis/a/0", "https://gis:8443/a/0")
          and not same_host("https://gis/a/0", "http://gis/a/0")
          and not same_host("https://gis/a/0", "https://other/a/0"),
          "another port, another scheme or another name is another host, and "
          "the --service token does not go to it  <-- pinned defect")
    check(not same_host("https://gis/a/0", None)
          and not same_host("https://gis/a/0", "baseline.json"),
          "and a --source that is not a url is on no host")
    check(not same_host("http://[bad/x/0", "http://[bad/y/0"),
          "and a url with an unclosed ipv6 bracket is on no host, rather than "
          "a ValueError raised before main() can catch it  <-- pinned defect")
    check(redact("x a-b y", None, "a-b", "") == "x %s y" % REDACTED,
          "redact takes several secrets, and skips the ones that are not set")

    # ---- the failure that arrives as HTTP 200
    check(error_text({"fields": []}) == "",
          "an ordinary layer definition carries no error")
    check(error_text({}) == "", "and neither does an empty body")
    check(error_text([]) == "", "or a body that is not an object at all")
    check(error_text({"error": "boom"}) == "",
          "an error that is not an object is not read as one")
    envelope = {"error": {"code": 498, "message": "Invalid Token",
                          "details": ["Invalid token."]}}
    check("Invalid Token" in error_text(envelope),
          "an ArcGIS error envelope is read out of a 200 response  "
          "<-- pinned defect")
    check("498" in error_text(envelope), "with its code")
    check("Invalid token." in error_text(envelope), "and its details")
    check(error_text({"error": {"code": 400, "message": "Bad",
                                "details": ["Bad"]}}).count("Bad") == 1,
          "a detail that repeats the message is not printed twice")
    check(error_text({"error": {"code": 500}})
          == "the service reported an error (code 500)",
          "an error with no message still reads as an error")
    check(error_text({"error": {"message": "Layer is offline"}})
          == "Layer is offline",
          "and an error with no code reads without an empty bracket after it")
    check(error_text({"error": {"message": "Bad", "details": 7}}) == "Bad 7",
          "details that are a number are printed as they stand, where they "
          "were iterated and raised a TypeError  <-- pinned defect")
    check(error_text({"error": {"message": "Bad", "details": ""}}) == "Bad",
          "and empty details print nothing")
    check(service_root_hint({"fields": []}) == "",
          "a layer definition needs no hint")
    check(service_root_hint({"layers": [{"id": 0}, {"id": 1}]})
          != "", "a service root gets one")
    check("/0" in service_root_hint({"layers": [{"id": 0}, {"id": 1}]}),
          "naming a layer id that exists  <-- pinned defect")
    check("/3" in service_root_hint({"layers": [{"id": 3}]}),
          "including when the first layer is not numbered zero")
    check(service_root_hint({"layers": []}) == "",
          "a service with no layers gets no hint")
    check(service_root_hint({}) == "", "and neither does an empty body")
    check(service_root_hint("x") == "", "or a body that is not an object")
    check(service_root_hint({"layers": ["Parcels"]}) != "",
          "a layers list of bare names still gets a hint")

    # ---- snapshots, in a real directory
    # A scheduled task exports these, and main() reads them. Left in place,
    # the caller's real token was sent to the stub and turned a check red.
    held_env = dict((name, os.environ.pop(name, None))
                    for name in (TOKEN_ENV, SOURCE_TOKEN_ENV))
    work = tempfile.mkdtemp(prefix="svcdrift-selftest-")
    try:
        check(TOKEN_ENV not in os.environ and SOURCE_TOKEN_ENV not in os.environ,
              "neither token variable is set while main() is driven, whatever "
              "the caller exported  <-- pinned defect")
        plain = os.path.join(work, "plain.json")
        with open(plain, "w", encoding="utf-8") as handle:
            json.dump(SOURCE, handle)
        check(len(normalize_layer(read_snapshot(plain))["fields"]) == 6,
              "a layer definition saved straight out of the rest browser is "
              "read as a snapshot")
        wrapped = os.path.join(work, "wrapped.json")
        document = snapshot_document(SOURCE, "https://gis/x/0?token=%s" % TOKEN,
                                     when="2026-09-16T00:00:00Z")
        check(TOKEN not in json.dumps(document),
              "the snapshot this tool writes carries no token  "
              "<-- pinned defect")
        check(document["url"] == "https://gis/x/0",
              "the url it records is the one without the query string")
        check(document["read"] == "2026-09-16T00:00:00Z",
              "and the time it was read")
        check(write_snapshot(document, wrapped) == wrapped,
              "writing a snapshot returns the path it wrote")
        check(os.path.isfile(wrapped), "and the file is there")
        check(read_snapshot(wrapped) == SOURCE,
              "and reading it back gives the layer definition it wrapped  "
              "<-- pinned defect")
        with open(wrapped, "r", encoding="utf-8") as handle:
            on_disk = handle.read()
        check(TOKEN not in on_disk, "with no token in the file on disk")
        check(on_disk.endswith("\n"),
              "and a newline at the end, so it reads in a terminal")
        nested = os.path.join(work, "sub", "dir", "deep.json")
        check(write_snapshot(document, nested) == nested,
              "a snapshot written into a directory that does not exist yet "
              "creates it")
        check(os.path.isfile(nested), "and lands there")
        check("could not be written" in fails(
            lambda: write_snapshot(document, work),
            "a snapshot aimed at a directory fails with a RuntimeError, where "
            "the OSError went past main() and exited 1  <-- pinned defect"),
              "and says it could not be written")
        broken = os.path.join(work, "broken.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        check("not valid json" in fails(lambda: read_snapshot(broken),
                                        "a snapshot that is not json fails"),
              "and says so rather than raising a decoder error at the caller")
        check("could not be read" in fails(
            lambda: read_snapshot(os.path.join(work, "absent.json")),
            "a snapshot that is not there fails"),
              "and says that instead")
        empty_json = os.path.join(work, "empty.json")
        with open(empty_json, "w", encoding="utf-8") as handle:
            handle.write("[]")
        raises(lambda: normalize_layer(read_snapshot(empty_json)),
               "a snapshot holding a json list raises when it is normalized")

        # ---- the arcpy shaping, driven with stand-ins
        class _Field(object):
            def __init__(self, name, type_, alias="", length=None, domain=""):
                self.name = name
                self.type = type_
                self.aliasName = alias
                self.length = length
                self.domain = domain

        class _Describe(object):
            baseName = "Parcels"
            shapeType = "Polygon"

            class spatialReference(object):
                factoryCode = 2272

        shaped = layer_from_arcpy(_Describe(), [
            _Field("OBJECTID", "OID"),
            _Field("OWNER", "String", "Owner Name", 80),
            _Field("STATUS", "String", "Status", 16, "StatusDomain"),
            _Field("ACRES", "Double", "Acres", 8)])
        check(shaped["name"] == "Parcels", "the dataset name is read")
        check(shaped["geometryType"] == "esriGeometryPolygon",
              "arcpy's Polygon becomes esriGeometryPolygon  <-- pinned defect")
        check(shaped["spatialReference"] == {"wkid": 2272},
              "and the factory code becomes a wkid")
        normal = normalize_layer(shaped, "dataset")
        check([f["type"] for f in normal["fields"]]
              == ["esriFieldTypeOID", "esriFieldTypeString",
                  "esriFieldTypeString", "esriFieldTypeDouble"],
              "every arcpy type folds to the rest spelling")
        check(normal["fields"][1]["alias"] == "Owner Name",
              "aliasName is read as the alias")
        check(normal["fields"][0]["alias"] == "OBJECTID",
              "and a field arcpy gives no alias for reports its own name")
        check(normal["fields"][2]["domain"] == ("name", "StatusDomain", ()),
              "arcpy's domain name is all there is, and it is read as a name")
        check(normal["fields"][3]["length"] is None,
              "the length arcpy reports for a double is dropped  "
              "<-- pinned defect")
        check("maxRecordCount" not in normal and "capabilities" not in normal,
              "a dataset reports no maxRecordCount and no capabilities, so "
              "neither is ever compared against a service  <-- pinned defect")
        table = layer_from_arcpy(type("T", (object,), {"baseName": "Owners"})(),
                                 [_Field("OBJECTID", "OID")])
        check("geometryType" not in table,
              "a table has no shape type, and none is invented for it  "
              "<-- pinned defect")
        check("spatialReference" not in table,
              "and no spatial reference either")
        check(diff_layers(normalize_layer(table), normalize_layer(shaped))
              != [], "and a table really is different from a feature class")
        unknown = layer_from_arcpy(
            type("T", (object,), {"baseName": "X", "shapeType": "Klein"})(),
            [])
        check(unknown["geometryType"] == "Klein",
              "a shape type this file has never heard of is passed through")

        # ---- the io layer, over a real socket on 127.0.0.1
        #
        # Everything above this is a pure function. This section stands a stub
        # http server up on a port the operating system picks, and drives the
        # reading, the error handling and main() itself through it. A network
        # path that has never been run is not a tested path.
        service_body = json.dumps(SERVICE)
        source_body = json.dumps(SOURCE)
        full_row = {"attributes": {"OBJECTID": 1, "PARCELID": "12345-001-00",
                                   "OWNER": "SMITH", "ACRES": 1.5,
                                   "LASTEDIT": 1767225600000, "ZONING": "R1"}}
        thin_row = {"attributes": {"OBJECTID": 1, "PARCELID": "12345-001-00",
                                   "ACRES": 1.5, "LASTEDIT": 1767225600000,
                                   "ZONING": "R1"}}
        not_found = json.dumps({"error": {"code": 404,
                                          "message": "Not Found"}})
        PLUS_TOKEN = "ab+/=cd" + TOKEN

        def aliased_as(alias):
            layer = json.loads(json.dumps(SOURCE))
            layer["fields"][2]["alias"] = alias
            return layer

        pages = {
            "/layer": (200, "application/json", service_body),
            "/layer/query": (200, "application/json",
                             json.dumps({"features": [full_row]})),
            "/origin": (200, "application/json", source_body),
            "/dropped": (200, "application/json", service_body),
            "/dropped/query": (200, "application/json",
                               json.dumps({"features": [thin_row]})),
            "/bare": (200, "application/json", service_body),
            "/bare/query": (200, "application/json",
                            json.dumps({"features": []})),
            "/root": (200, "application/json",
                      json.dumps({"name": "Parcels",
                                  "layers": [{"id": 0, "name": "Parcels"}]})),
            "/boom": (200, "application/json",
                      json.dumps({"error": {"code": 500,
                                            "message": "Layer is offline",
                                            "details": ["restart it"]}})),
            "/page": (200, "text/html", "<html><body>a layer</body></html>"),
            "/nolist": (200, "application/json", service_body),
            "/nolist/query": (200, "application/json", json.dumps({"count": 0})),
            "/slow": (200, "application/json", service_body),
            "/styled": (200, "application/json", json.dumps(STYLED_SOURCE)),
            "/restyled": (200, "application/json",
                          json.dumps(STYLED_SERVICE)),
            "/republished": (200, "application/json",
                             json.dumps(STYLED_AGAIN)),
            "/listy/query": (200, "application/json", "[]"),
            "/badstyle": (200, "application/json",
                          json.dumps(dict(SOURCE, drawingInfo="renderer"))),
            "/unicode": (200, "application/json", json.dumps(
                aliased_as("Owner \u2265 3 \u2192 lot"))),
            "/echo": (200, "application/json", json.dumps(dict(
                aliased_as("Owner " + TOKEN), drawingInfo={"renderer": {
                    "type": "simple", "symbol": {
                        "type": "esriPMS",
                        "url": "http://127.0.0.1/img.png?token=" + TOKEN}}}))),
            "/slashed": (200, "application/json", json.dumps(
                aliased_as("Owner " + PLUS_TOKEN)).replace("/", "\\/")),
            "/uescaped": (200, "application/json", json.dumps(
                aliased_as("Owner " + TOKEN)).replace(
                    TOKEN, "\\u0053" + TOKEN[1:])),
            "/encoded": (200, "application/json", json.dumps({"error": {
                "code": 498, "message": "Invalid token",
                "details": ["token %s expired"
                            % urllib.parse.quote_plus(PLUS_TOKEN)]}})),
        }
        # path -> where the stub sends a 301, filled in once both ports exist.
        redirects = {}
        hits = []
        # (port, path) of every request, so a test can see which of the two
        # stub servers a token went to.
        landed = []

        class _Stub(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                hits.append(self.path)
                landed.append((self.server.server_address[1], self.path))
                path, _sep, raw = self.path.partition("?")
                params = urllib.parse.parse_qs(raw)
                if path in redirects:
                    self.send_response(301)
                    self.send_header("Location", redirects[path] + "?" + raw)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/short":
                    # A proxy that cut the body short.
                    self.send_response(200)
                    self.send_header("Content-Length", "5000")
                    self.end_headers()
                    self.wfile.write(b'{"fields": [')
                    return
                if path == "/nothttp":
                    # A port that answers with another protocol.
                    self.wfile.write(b"SSH-2.0-OpenSSH_9.6\r\n")
                    return
                if path == "/slow":
                    time.sleep(0.6)
                if path == "/secret":
                    if params.get("token", [""])[0] != TOKEN:
                        reply = (200, "application/json", json.dumps(
                            {"error": {"code": 499, "message": "Token Required",
                                       "details": ["Token Required"]}}))
                    else:
                        reply = (200, "application/json", service_body)
                else:
                    reply = pages.get(path, (404, "application/json",
                                             not_found))
                body = reply[2].encode("utf-8")
                self.send_response(reply[0])
                self.send_header("Content-Type", reply[1])
                self.send_header("Content-Length", "%d" % len(body))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        class _Quiet(http.server.ThreadingHTTPServer):
            """A client that timed out leaves a half written reply behind, and
            socketserver prints that to stderr. Silenced, so a green run is
            green."""
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        server = _Quiet(("127.0.0.1", 0), _Stub)
        thread = threading.Thread(target=server.serve_forever)
        thread.daemon = True
        thread.start()
        # A second stub on another port is another host to same_host(), which
        # is how the token that must not follow a --source there is tested.
        other_server = _Quiet(("127.0.0.1", 0), _Stub)
        other_thread = threading.Thread(target=other_server.serve_forever)
        other_thread.daemon = True
        other_thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            other = "http://127.0.0.1:%d" % other_server.server_address[1]
            redirects.update({"/hop": other + "/layer",
                              "/moved": base + "/layer",
                              "/ftp": "ftp://127.0.0.1/layer"})
            check(server.server_address[0] == "127.0.0.1",
                  "the stub server is bound to loopback and nothing else")
            check(server.socket.family == socket.AF_INET, "over ipv4")

            live = fetch_json(base + "/layer", {})
            check(len(live["fields"]) == 6,
                  "a layer definition is read over a real socket")
            check(hits[-1].endswith("f=json"),
                  "and the request that read it asked for json  "
                  "<-- pinned defect")
            check(len(normalize_layer(read_service(base + "/layer"))["fields"])
                  == 6, "read_service gives a definition that normalizes")
            check(diff_layers(normalize_layer(read_service(base + "/origin")),
                              normalize_layer(read_service(base + "/layer")))
                  == found,
                  "two services read over the wire give the same six "
                  "differences as the two fixtures  <-- pinned defect")

            message = fails(lambda: read_service(base + "/root"),
                            "a url that names the service rather than a layer "
                            "fails")
            check("/0" in message,
                  "and the message says to add a layer id  <-- pinned defect")
            message = fails(lambda: read_service(base + "/boom"),
                            "an error envelope arriving as HTTP 200 fails")
            check("Layer is offline" in message,
                  "with the message the server put in the body, which a client "
                  "that checks the status code never sees  <-- pinned defect")
            check("restart it" in message, "and the detail under it")
            message = fails(lambda: read_service(base + "/page"),
                            "a layer url that answers with html fails")
            check("did not return json" in message,
                  "and says so rather than raising a json decoder error")
            check("<html>" in message,
                  "quoting what came back, so the cause is visible")
            message = fails(lambda: read_service(base + "/missing"),
                            "a url that answers 404 fails")
            check("404" in message, "with the status code in the message")

            check(len(query_one(base + "/layer")) == 1,
                  "a query over the wire returns a feature")
            check(any("returnGeometry=false" in h for h in hits),
                  "and it asked for no geometry, because the drift is in the "
                  "attributes and a polygon per row is bandwidth spent on "
                  "nothing  <-- pinned defect")
            check(query_one(base + "/bare") == [],
                  "a layer with no rows returns an empty list rather than "
                  "failing  <-- pinned defect")
            fails(lambda: query_one(base + "/nolist"),
                  "a query that answers with no features list fails")
            fails(lambda: query_one(base + "/listy"),
                  "and so does one that answers with a json list, where "
                  ".get() on the list raised an AttributeError  "
                  "<-- pinned defect")
            check(len(query_one(base + "/layer?f=html")) == 1,
                  "a layer url pasted straight out of the rest browser is "
                  "still queried, rather than /query landing inside the "
                  "?f=html and the probe reading the layer definition  "
                  "<-- pinned defect")
            check(probe_fields(normalize_layer(read_service(base + "/layer")),
                               query_one(base + "/layer")) == [],
                  "a service that returns everything it advertises probes "
                  "clean over the wire")
            probed = probe_fields(
                normalize_layer(read_service(base + "/dropped")),
                query_one(base + "/dropped"))
            check(kinds(probed) == ["FIELD_NOT_RETURNED"],
                  "and a service that advertises a field its data does not "
                  "carry is caught over the wire  <-- pinned defect")
            check(probed[0].target == "OWNER", "with the field named")

            secret = fails(lambda: read_service(base + "/secret"),
                           "a layer that needs a token fails without one")
            check("Token Required" in secret,
                  "saying what the server said, which is a 499 in a 200")
            check(len(read_service(base + "/secret", TOKEN)["fields"]) == 6,
                  "and reads with the token  <-- pinned defect")
            check(any("token=" in h for h in hits),
                  "the token really did go out on the query string")
            check(TOKEN not in secret,
                  "and no failure message carries it  <-- pinned defect")

            spare = socket.socket()
            spare.bind(("127.0.0.1", 0))
            dead = spare.getsockname()[1]
            spare.close()
            message = fails(
                lambda: read_service("http://127.0.0.1:%d/layer" % dead, TOKEN),
                "a port with nothing listening on it fails")
            check(TOKEN not in message,
                  "and the connection error carries no token, although urllib "
                  "puts the url it could not open into its own message  "
                  "<-- pinned defect")
            message = fails(
                lambda: read_service(base + "/slow", TOKEN, 0.15),
                "a server slower than --timeout fails")
            check(TOKEN not in message, "and that message carries no token")

            # ---- main(), end to end against the stub
            baseline = os.path.join(work, "baseline.json")
            with open(baseline, "w", encoding="utf-8") as handle:
                json.dump(SOURCE, handle)

            code, printed = captured(
                lambda: main(["--service", base + "/layer",
                              "--source", baseline]))
            check(code == 1,
                  "a run against a service that drifted exits 1  "
                  "<-- pinned defect")
            check("VERDICT: BREAK" in printed, "and prints the verdict")
            check(printed.count("BREAK   ") == 3,
                  "and the three breaking lines")
            code, printed = captured(
                lambda: main(["--service", base + "/origin",
                              "--source", baseline]))
            check(code == 0,
                  "a run against a service that matches exits 0  "
                  "<-- pinned defect")
            check("VERDICT: MATCH" in printed, "and says so")
            check("no differences" in printed, "and prints nothing else")

            warn_only_source = os.path.join(work, "alias.json")
            aliased = json.loads(source_body)
            aliased["fields"][2]["alias"] = "Owner"
            with open(warn_only_source, "w", encoding="utf-8") as handle:
                json.dump(aliased, handle)
            code, printed = captured(
                lambda: main(["--service", base + "/origin",
                              "--source", warn_only_source]))
            check(code == 0,
                  "an alias-only difference does NOT fail a run  "
                  "<-- pinned defect")
            check("ALIAS_CHANGED" in printed, "although it is still reported")
            code, printed = captured(
                lambda: main(["--service", base + "/origin", "--source",
                              warn_only_source, "--strict"]))
            check(code == 1, "and --strict makes the same run fail")
            check("VERDICT: WARN" in printed,
                  "while the verdict stays WARN, because nothing broke  "
                  "<-- pinned defect")

            code, printed = captured(
                lambda: main(["--service", base + "/layer", "--source",
                              baseline, "--json"]))
            document = json.loads(printed)
            check(code == 1, "--json exits the same way the text report does")
            check(document["verdict"] == "BREAK",
                  "and writes a document a script can read")
            check(len(document["differences"]) == 6, "carrying all six")
            check(document["exitCode"] == code,
                  "and the exit code the run actually used  <-- pinned defect")

            code, printed = captured(
                lambda: main(["--service", base + "/dropped", "--probe"]))
            check(code == 1,
                  "--probe on its own fails when the data does not carry an "
                  "advertised field  <-- pinned defect")
            check("FIELD_NOT_RETURNED" in printed, "and says which field")
            check("probe: 1 feature(s)" in printed,
                  "and how many rows it looked at")
            code, printed = captured(
                lambda: main(["--service", base + "/dropped?f=html",
                              "--probe"]))
            check(code == 1,
                  "and the same run works end to end on the url as it was "
                  "pasted, query string and all  <-- pinned defect")
            check("FIELD_NOT_RETURNED" in printed,
                  "finding the same missing field, rather than exiting 2 on a "
                  "features list the layer definition never had")
            code, printed = captured(
                lambda: main(["--service", base + "/bare", "--probe"]))
            check(code == 0, "a layer with no rows cannot be probed")
            check("probe: 0 feature(s)" in printed,
                  "and the report says so rather than claiming it is clean  "
                  "<-- pinned defect")
            code, printed = captured(
                lambda: main(["--service", base + "/dropped", "--source",
                              baseline, "--probe"]))
            check(code == 1, "a comparison and a probe run together")
            check("FIELD_NOT_RETURNED" in printed and "FIELD_REMOVED" in printed,
                  "and both kinds of finding land in one report")

            out_path = os.path.join(work, "written.json")
            code, printed = captured(
                lambda: main(["--service", base + "/layer", "--out", out_path]))
            check(code == 0, "--out without --apply exits on the read alone")
            check(not os.path.exists(out_path),
                  "and writes nothing  <-- pinned defect")
            check("Re-run with --apply" in printed, "saying what would happen")
            code, printed = captured(
                lambda: main(["--service", base + "/layer", "--out", out_path,
                              "--apply"]))
            check(code == 0 and os.path.isfile(out_path),
                  "--apply writes the snapshot")
            check(len(normalize_layer(read_snapshot(out_path))["fields"]) == 6,
                  "and it reads back as the layer that was published")
            code, printed = captured(
                lambda: main(["--service", base + "/origin",
                              "--source", out_path]))
            check(code == 1,
                  "a snapshot taken today is a baseline to compare against "
                  "tomorrow  <-- pinned defect")

            json_out = os.path.join(work, "viajson.json")
            noise, sys.stderr = sys.stderr, io.StringIO()
            try:
                code, printed = captured(
                    lambda: main(["--service", base + "/layer", "--json",
                                  "--out", json_out, "--apply"]))
                note = sys.stderr.getvalue()
            finally:
                sys.stderr = noise
            check(json.loads(printed)["verdict"] == "MATCH",
                  "--json together with --out still writes json and only json "
                  "on stdout  <-- pinned defect")
            check("wrote " in note,
                  "and the note about the baseline goes to stderr instead, "
                  "where it cannot break a script parsing the report")
            check(os.path.isfile(json_out), "and the baseline was written")

            code, printed = captured(
                lambda: main(["--service", base + "/layer", "--source",
                              base + "/origin"]))
            check(code == 1,
                  "a service against another service needs no file and no "
                  "arcpy at all  <-- pinned defect")

            # ---- symbology end to end, over the wire
            code, printed = captured(
                lambda: main(["--service", base + "/restyled", "--source",
                              base + "/styled"]))
            check(code == 1,
                  "a service whose renderer drifted from another service's "
                  "exits 1 over the wire  <-- pinned defect")
            check("3 break(s), 6 warning(s)" in printed,
                  "with the nine seeded differences in the report")
            check("CLASS_SYMBOL_CHANGED" in printed
                  and "VISIBILITY_NARROWED" in printed,
                  "the recoloured class and the narrowed range among them")
            code, printed = captured(
                lambda: main(["--service", base + "/republished", "--source",
                              base + "/styled"]))
            check(code == 0 and "no differences" in printed,
                  "a service republished with nothing but JSON noise in its "
                  "drawingInfo exits 0 and prints no differences  "
                  "<-- pinned defect")
            code, printed = captured(
                lambda: main(["--service", base + "/restyled", "--source",
                              base + "/styled", "--schema-only"]))
            check(code == 0 and "VERDICT: MATCH" in printed,
                  "--schema-only compares the way version 1.0 did, and the "
                  "same drift passes  <-- pinned defect")
            styled_out = os.path.join(work, "styled.json")
            code, printed = captured(
                lambda: main(["--service", base + "/styled", "--out",
                              styled_out, "--apply"]))
            with open(styled_out, "r", encoding="utf-8") as handle:
                styled_saved = json.load(handle)
            check(code == 0 and "drawingInfo" in styled_saved["layer"],
                  "a baseline written today keeps the renderer")
            code, printed = captured(
                lambda: main(["--service", base + "/restyled", "--source",
                              styled_out, "--json"]))
            document = json.loads(printed)
            check(code == 1 and len(document["differences"]) == 9,
                  "and a service compared against that baseline tomorrow "
                  "finds the same nine differences  <-- pinned defect")
            check(sorted(d["kind"] for d in document["differences"])
                  == kinds(restyled),
                  "of the same kinds, in the json report a script reads")

            # ---- read_dataset, with a stand-in module where arcpy would be
            fake = types.ModuleType("arcpy")
            fake.Exists = lambda path: path == "fake.gdb/Parcels"
            fake.Describe = lambda path: _Describe()
            fake.ListFields = lambda path: [_Field("OBJECTID", "OID"),
                                            _Field("STATUS", "String", "", 16)]
            held_arcpy = sys.modules.pop("arcpy", None)
            sys.modules["arcpy"] = fake
            try:
                dataset = read_dataset("fake.gdb/Parcels")
                check(dataset["geometryType"] == "esriGeometryPolygon"
                      and len(dataset["fields"]) == 2,
                      "read_dataset shapes what arcpy hands back, driven with "
                      "a stand-in module where arcpy would be")
                check("drawingInfo" not in dataset,
                      "and a feature class read through arcpy carries no "
                      "renderer")
                check("does not exist" in fails(
                    lambda: read_dataset("fake.gdb/Absent"),
                    "a dataset arcpy cannot find fails"), "and says so")
                code, printed = captured(
                    lambda: main(["--service", base + "/restyled", "--source",
                                  "fake.gdb/Parcels"]))
                check(code == 0 and "no differences" in printed,
                      "a feature class against a service whose renderer "
                      "drifted reports no symbology, because the feature "
                      "class has none to compare  <-- pinned defect")
            finally:
                del sys.modules["arcpy"]
                sys.modules.update({"arcpy": held_arcpy} if held_arcpy else {})
            check(sys.modules.get("arcpy") is not fake,
                  "and the stand-in is gone from sys.modules again")

            # ---- the token out of the environment, driven for real
            #
            # A scheduled task cannot put a token on a command line, where
            # every process on the box reads it out of the process table. The
            # environment variable is the whole point, so it is run rather
            # than described.
            env_out = os.path.join(work, "secret.json")
            try:
                noise, sys.stderr = sys.stderr, io.StringIO()
                try:
                    code, printed = captured(
                        lambda: main(["--service", base + "/secret",
                                      "--out", env_out]))
                    refused = sys.stderr.getvalue()
                finally:
                    sys.stderr = noise
                check(code == 2,
                      "a secured layer with no token on the command line and "
                      "none in the environment exits 2")
                check("Token Required" in refused,
                      "saying what the server asked for")
                os.environ[TOKEN_ENV] = TOKEN
                code, printed = captured(
                    lambda: main(["--service", base + "/secret",
                                  "--out", env_out, "--apply"]))
                check(code == 0,
                      "and the same run reads the layer with the token taken "
                      "from %s  <-- pinned defect" % TOKEN_ENV)
                check(TOKEN not in printed,
                      "with no token anywhere on stdout  <-- pinned defect")
                with open(env_out, "r", encoding="utf-8") as handle:
                    written = handle.read()
                check(TOKEN not in written,
                      "and none in the baseline it wrote  <-- pinned defect")
                check("secret" in json.loads(written)["url"],
                      "although the url it read is in there")
                os.environ[TOKEN_ENV] = "not-the-token"
                code, printed = captured(
                    lambda: main(["--service", base + "/secret", "--token",
                                  TOKEN, "--out", env_out]))
                check(code == 0,
                      "--token on the command line beats a stale one in the "
                      "environment  <-- pinned defect")
            finally:
                os.environ.pop(TOKEN_ENV, None)

            def run(argv):
                """main() with stdout and stderr both collected."""
                noise, sys.stderr = sys.stderr, io.StringIO()
                try:
                    code, printed = captured(lambda: main(argv))
                    return code, printed, sys.stderr.getvalue()
                finally:
                    sys.stderr = noise

            # ---- whose token goes where
            other_port = other_server.server_address[1]
            try:
                code, printed, said = run(
                    ["--service", base + "/secret", "--token", TOKEN,
                     "--source", other + "/secret"])
                check(code == 2 and "Token Required" in said,
                      "the --service token is not sent to a --source on "
                      "another host, so a secured source there is refused  "
                      "<-- pinned defect")
                check(not any(port == other_port and "token=" in path
                              for port, path in landed),
                      "and no request to that host carried a token  "
                      "<-- pinned defect")
                check(TOKEN not in printed + said,
                      "and nothing printed carries it")
                code, printed, said = run(
                    ["--service", base + "/secret", "--token", TOKEN,
                     "--source", other + "/secret", "--source-token", TOKEN])
                check(code == 0,
                      "--source-token gives the source host a token of its own")
                check(TOKEN not in printed + said,
                      "and it is not printed either")
                os.environ[SOURCE_TOKEN_ENV] = TOKEN
                code, printed, said = run(
                    ["--service", base + "/secret", "--token", TOKEN,
                     "--source", other + "/secret"])
                check(code == 0, "and so does %s" % SOURCE_TOKEN_ENV)
                os.environ.pop(SOURCE_TOKEN_ENV)
                code, printed, said = run(
                    ["--service", base + "/secret", "--token", TOKEN,
                     "--source", base + "/secret"])
                check(code == 0,
                      "while a --source on the --service host is sent the "
                      "--service token, as it always was")
            finally:
                os.environ.pop(SOURCE_TOKEN_ENV, None)
            mark = len(landed)
            code, printed, said = run(
                ["--service", base + "/layer?token=" + TOKEN,
                 "--source", base + "/origin?token=" + TOKEN])
            check(code == 1 and TOKEN not in printed + said,
                  "a token pasted into the --service or the --source url is "
                  "not printed in the report  <-- pinned defect")
            check(len(landed) > mark
                  and not any("token=" in path for _port, path in landed[mark:]),
                  "and it is not sent either, because the query string of a "
                  "layer url is dropped before the read")

            # ---- what must never be printed, and what must never exit 1
            code, printed, said = run(["--service", base + "/a b/layer",
                                       "--token", TOKEN, "--probe"])
            check(code == 2 and "could not be read" in said
                  and TOKEN not in printed + said,
                  "a --service url with a space in it exits 2 with the token "
                  "taken out, where http.client's InvalidURL was a traceback "
                  "that quoted the token and exited 1  <-- pinned defect")
            code, printed, said = run(["--service", base + "/layer",
                                       "--token", TOKEN,
                                       "--source", base + "/a b/origin"])
            check(code == 2 and TOKEN not in printed + said,
                  "and so does a --source url with a space in it, which is "
                  "sent the same token  <-- pinned defect")
            code, printed, said = run(["--service", "http://[bad/x/0",
                                       "--source", "http://[bad/y/0"])
            check(code == 2 and "IPv6" in said,
                  "a --service and --source with an unclosed ipv6 bracket "
                  "exit 2, not 1  <-- pinned defect")
            code, printed, said = run(["--service",
                                       "http://127.0.0.1:abc/layer", "--probe"])
            check(code == 2 and "nonnumeric port" in said,
                  "a port that is not a number exits 2, not 1  "
                  "<-- pinned defect")
            for path, what in (("/short", "a body cut short by a proxy"),
                               ("/nothttp", "a port that answers in another "
                                            "protocol")):
                message = fails(
                    lambda: read_service(base + path, TOKEN),
                    "%s fails with a RuntimeError, where http.client's own "
                    "exception went past main() and exited 1  "
                    "<-- pinned defect" % what)
                check(TOKEN not in message, "and the message carries no token")

            mark = len(landed)
            message = fails(lambda: read_service(base + "/hop", TOKEN),
                            "a redirect to another host is not followed  "
                            "<-- pinned defect")
            check("another host" in message and TOKEN not in message,
                  "and the message says why, with no token in it")
            check(len(landed) > mark
                  and not any(port == other_port for port, _p in landed[mark:]),
                  "no request, and so no token, reached the other host  "
                  "<-- pinned defect")
            fails(lambda: read_service(base + "/ftp", TOKEN),
                  "a redirect to ftp is not followed either")
            check(len(read_service(base + "/moved", TOKEN)["fields"]) == 6,
                  "while a redirect on the same host is followed")

            echo_out = os.path.join(work, "echo.json")
            code, printed, said = run(["--service", base + "/echo", "--token",
                                       TOKEN, "--source", baseline,
                                       "--out", echo_out, "--apply"])
            with open(echo_out, "r", encoding="utf-8") as handle:
                echoed = handle.read()
            check(code == 0 and "ALIAS_CHANGED" in printed
                  and TOKEN not in printed + said + echoed,
                  "a token the service echoes back in an alias and a symbol "
                  "url reaches neither the report nor the baseline  "
                  "<-- pinned defect")
            check(REDACTED in echoed and json.loads(echoed)["layer"],
                  "the baseline is still json, with the token marked as taken "
                  "out")
            code, printed, said = run(["--service", base + "/echo", "--token",
                                       TOKEN, "--source", baseline, "--json"])
            check(code == 0 and TOKEN not in printed
                  and json.loads(printed)["warnings"] == 1,
                  "and none reaches the --json report either")
            for path, secret, how in (("/slashed", PLUS_TOKEN, "\\/"),
                                      ("/uescaped", TOKEN, "\\u0053")):
                read = json.dumps(read_service(base + path, secret),
                                  ensure_ascii=False)
                check(secret not in read and REDACTED in read,
                      "a token the service echoes back behind a %s escape is "
                      "taken out of the parsed layer too, so --out cannot "
                      "write it  <-- pinned defect" % how)
            message = fails(lambda: read_service(base + "/encoded", PLUS_TOKEN),
                            "an error envelope that echoes the token back "
                            "url-encoded fails")
            check(TOKEN not in message
                  and urllib.parse.quote_plus(PLUS_TOKEN) not in message,
                  "and the encoded form is taken out of its message too  "
                  "<-- pinned defect")
            stale = os.path.join(work, "stale.json")
            with open(stale, "w", encoding="utf-8") as handle:
                json.dump(aliased_as("Owner " + TOKEN), handle)
            for extra, what in (([], "text"), (["--json"], "--json")):
                code, printed, said = run(["--service", base + "/origin",
                                           "--token", TOKEN,
                                           "--source", stale] + extra)
                check(code == 0 and TOKEN not in printed + said
                      and "ALIAS_CHANGED" in printed,
                      "a baseline an older run wrote with the token in it is "
                      "compared, and the %s report still prints no token  "
                      "<-- pinned defect" % what)
            check(json.loads(redact('{"u": "a?token=abc\\" q"}'))
                  == {"u": "a?token=%s\" q" % REDACTED},
                  "a token= value in a json body stops at a backslash, so the "
                  "escaped quote after it survives and the body still parses")

            password = "PassW0rdXYZ"
            code, printed, said = run(
                ["--service", "http://svcuser:%s@127.0.0.1:9/a/0" % password,
                 "--probe"])
            check(code == 64 and password not in printed + said,
                  "a --service url with a password in it is refused as a "
                  "usage error without printing it, where the read failed and "
                  "the error quoted the password  <-- pinned defect")
            code, printed, said = run(
                ["--service", base + "/layer", "--source",
                 "https://svcuser:%s@gis.example/x/0" % password])
            check(code == 64 and password not in printed + said,
                  "and so is a --source url with one")
            check(has_userinfo(" HTTPS://svcuser@gis.example/x/0")
                  and not has_userinfo(base + "/layer?next=a@b")
                  and not has_userinfo(None),
                  "a user name is found before the host only, and an @ in the "
                  "query string is not one")

            wide = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
            noise, sys.stdout = sys.stdout, wide
            quiet, sys.stderr = sys.stderr, io.StringIO()
            try:
                code = main(["--service", base + "/unicode",
                             "--source", baseline])
                wide.flush()
                raw = wide.buffer.getvalue()
            finally:
                sys.stdout, sys.stderr = noise, quiet
            check(code == 0 and b"ALIAS_CHANGED" in raw and b"\\u2265" in raw,
                  "an alias holding a character a cp1252 pipe cannot encode is "
                  "printed escaped, and the run exits 0 for its one warning, "
                  "where print() raised UnicodeEncodeError and exited 1  "
                  "<-- pinned defect")

            # ---- the exits that are not a verdict
            code, printed, said = run(["--service", base + "/badstyle",
                                       "--source", baseline])
            check(code == 2 and "drawingInfo" in said,
                  "a service whose drawingInfo cannot be read exits 2")
            code, printed, said = run(["--service", base + "/badstyle",
                                       "--source", baseline, "--schema-only"])
            check(code == 0 and "VERDICT: MATCH" in printed,
                  "and --schema-only never reads it, as version 1.0 did not  "
                  "<-- pinned defect")
            code, printed, said = run(["--service", base + "/layer",
                                       "--out", work, "--apply"])
            check(code == 2 and "could not be written" in said,
                  "an --out that cannot be written exits 2, not 1, which is "
                  "the code for a break  <-- pinned defect")
            code, printed, said = run(["--service", base + "/origin",
                                       "--source", baseline, "--timeout",
                                       "%d" % MAX_TIMEOUT])
            check(code == 0, "the longest --timeout allowed is accepted")

            noise, sys.stderr = sys.stderr, io.StringIO()
            try:
                code, printed = captured(
                    lambda: main(["--service", base + "/boom", "--source",
                                  baseline]))
                check(code == 2,
                      "a service that could not be read exits 2, not 1: no "
                      "answer is not the same as no drift  <-- pinned defect")
                check("Layer is offline" in sys.stderr.getvalue(),
                      "with the server's own message on stderr")
                code, printed = captured(
                    lambda: main(["--service", base + "/layer", "--source",
                                  os.path.join(work, "absent.json")]))
                check(code == 2, "a source that could not be read exits 2 too")
                code, printed = captured(
                    lambda: main(["--service", base + "/layer", "--source",
                                  os.path.join(work, "parcels.gdb", "P")]))
                check(code == 2,
                      "and a dataset source that cannot be opened exits 2 with "
                      "a remedy rather than a traceback")
                check("arcpy" in sys.stderr.getvalue(),
                      "naming arcpy, which is what is missing")
                token_text = sys.stderr.getvalue()
            finally:
                sys.stderr = noise
            check(TOKEN not in token_text,
                  "and nothing on stderr carries a token")
        finally:
            for stub, runner in ((server, thread),
                                 (other_server, other_thread)):
                stub.shutdown()
                stub.server_close()
                runner.join(timeout=5)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for name in (TOKEN_ENV, SOURCE_TOKEN_ENV):
            os.environ.pop(name, None)
        os.environ.update(dict((name, value) for name, value
                               in held_env.items() if value is not None))
    check(all(os.environ.get(name) == held_env[name]
              for name in (TOKEN_ENV, SOURCE_TOKEN_ENV)),
          "and the caller's own %s and %s are put back as they were  "
          "<-- pinned defect" % (TOKEN_ENV, SOURCE_TOKEN_ENV))

    # ---- the command line
    clean = _parse([])
    check(clean.service is None, "--service has no default")
    check(clean.source is None, "--source has no default")
    check(clean.token is None,
          "--token has no default, so a token is never guessed at")
    check(clean.out is None, "--out has no default")
    check(clean.apply is False,
          "--apply defaults to OFF, so nothing is ever written by accident  "
          "<-- pinned defect")
    check(clean.probe is False,
          "--probe defaults to OFF, so no run queries rows unless it was "
          "asked to  <-- pinned defect")
    check(clean.strict is False,
          "--strict defaults to OFF, so an alias change does not fail a run")
    check(clean.json is False, "--json defaults to OFF")
    check(clean.schema_only is False,
          "--schema-only defaults to OFF, so the renderer is compared unless a "
          "run opts out  <-- pinned defect")
    check(_parse(["--schema-only"]).schema_only is True,
          "--schema-only is read")
    check(clean.self_test is False, "--self-test defaults to OFF")
    check(clean.timeout == 60,
          "--timeout defaults to 60 seconds, the number the README documents. "
          "Written out rather than compared against HTTP_TIMEOUT, which is "
          "the constant that sets it and would agree with any value  "
          "<-- pinned defect")
    check(HTTP_TIMEOUT == 60,
          "and that is the constant the whole file reads from")
    check(TOKEN_ENV == "SVCDRIFT_TOKEN",
          "the token environment variable is named SVCDRIFT_TOKEN, which is "
          "the name a scheduled task has to export  <-- pinned defect")
    check(_parse(["--service", "https://gis/x/0"]).service == "https://gis/x/0",
          "--service is read")
    check(_parse(["--source", "a.json"]).source == "a.json", "--source is read")
    check(_parse(["--token", "abc"]).token == "abc", "--token is read")
    check(_parse(["--out", "b.json"]).out == "b.json", "--out is read")
    check(_parse(["--apply"]).apply is True, "--apply is read")
    check(_parse(["--probe"]).probe is True, "--probe is read")
    check(_parse(["--strict"]).strict is True, "--strict is read")
    check(_parse(["--json"]).json is True, "--json is read")
    check(_parse(["--self-test"]).self_test is True, "--self-test is read")
    check(_parse(["--timeout", "5"]).timeout == 5.0, "--timeout is read")
    check(_parse(["--timeout", "0.5"]).timeout == 0.5,
          "and it takes a fraction of a second, which the self-test uses")
    refuses(["--timeout", "soon"], "a --timeout that is not a number is refused")
    refuses(["--service"], "a --service with no value is refused")
    refuses(["--apply", "yes"], "--apply takes no value")
    refuses(["--drop-everything"], "a flag that does not exist is refused")
    refuses(["--self-test", "--extra"], "and so is a stray argument")

    def exits(argv):
        noise, sys.stderr = sys.stderr, io.StringIO()
        try:
            return captured(lambda: main(argv))[0]
        finally:
            sys.stderr = noise

    check(exits([]) == 64, "a run with no --service is a usage error")
    check(exits(["--service", "C:/data/x.gdb/Parcels", "--source", "a.json"])
          == 64,
          "a --service that is not an http url is a usage error: the service "
          "side is always the published one  <-- pinned defect")
    check(exits(["--service", "https://gis/x/0"]) == 64,
          "a run with nothing to compare against and nothing to probe is a "
          "usage error, not a silent success  <-- pinned defect")
    check(exits(["--service", "https://gis/x/0", "--source", "a.json",
                 "--apply"]) == 64,
          "--apply with no --out is a usage error, because there is nothing "
          "for it to authorise  <-- pinned defect")
    check(exits(["--service", "https://gis/x/0", "--apply"]) == 64,
          "and --apply on its own is one for the same reason")
    check(exits(["--service", "https://gis/x/0", "--source", "a.json",
                 "--timeout", "0"]) == 64,
          "a --timeout of zero is a usage error")
    check(exits(["--service", "https://gis/x/0", "--source", "a.json",
                 "--timeout", "-1"]) == 64, "and so is a negative one")
    check(exits(["--service", "https://gis/x/0", "--source", "  "]) == 64,
          "and so is a blank --source")
    for value, what in (("inf", "an infinite --timeout, which was an "
                                "OverflowError that exited 1"),
                        ("nan", "a --timeout of nan"),
                        ("%d" % (MAX_TIMEOUT + 1), "one over a day")):
        check(exits(["--service", "https://gis/x/0", "--source", "a.json",
                     "--timeout", value]) == 64,
              "%s is a usage error  <-- pinned defect" % what)

    def usage_code(argv):
        noise, sys.stderr = sys.stderr, io.StringIO()
        try:
            # A return is tagged, so main()'s own 64 cannot pass for argparse's.
            return "returned %r" % (main(argv),)
        except SystemExit as exc:
            return exc.code
        finally:
            sys.stderr = noise

    check(usage_code(["--timeout", "soon"]) == 64,
          "argparse's own usage error exits 64 as documented, not the 2 that "
          "means a side could not be read  <-- pinned defect")
    check(_parse(["--source-token", "abc"]).source_token == "abc"
          and clean.source_token is None,
          "--source-token is read, and has no default")
    check(SOURCE_TOKEN_ENV == "SVCDRIFT_SOURCE_TOKEN",
          "the source token environment variable is SVCDRIFT_SOURCE_TOKEN")

    # ---- the harness itself, which is the thing every other line trusts
    def probe():
        check(False, "a false check must be recorded as a failure")
        raises(lambda: None, "a function that raises nothing must fail")
        raises(lambda: 1 / 0, "a function that raises the wrong thing must fail")
        fails(lambda: None, "a call that does not raise must fail")
        fails(lambda: 1 / 0, "a call that raises the wrong thing must fail")
        refuses(["--self-test"], "an argv argparse accepts must fail")

    kept_passed, kept_failed = passed[0], list(failed)
    _result, noise = captured(probe)
    probe_passed, probe_failed = passed[0], list(failed)
    passed[0], failed[:] = kept_passed, kept_failed
    check(len(probe_failed) - len(kept_failed) == 6,
          "the harness records a false check, a missing exception, two wrong "
          "exceptions, a call that did not raise and an argv argparse "
          "accepted as six failures, so a broken tool turns this self-test "
          "red  <-- pinned defect")
    check(probe_passed == kept_passed,
          "not one of those six was counted as a pass")
    check(noise.count("FAIL  ") == 6,
          "and every one of them printed a FAIL line an operator can see")

    # ---- arcpy is optional, and the file itself says so
    with open(__file__, "r", encoding="utf-8") as handle:
        own_source = handle.read()
    arcpy_lines = [line for line in own_source.splitlines()
                   if line.strip().startswith("import arcpy")]
    check(len(arcpy_lines) == 1, "there is exactly one import of arcpy")
    check(arcpy_lines[0].startswith("        "),
          "and it is indented inside read_dataset, so nothing else in this "
          "file can reach it and --self-test never imports it  "
          "<-- pinned defect")

    # ---- the file as a module, and the lines that end a run
    namespace = runpy.run_path(__file__, run_name="svcdrift_as_a_module")
    check(callable(namespace["main"]) and namespace["VERSION"] == VERSION,
          "the file loads as a module without running main(), so another "
          "script can import it and call diff_layers itself")
    check(summary_lines(7, []) == ["7 assertions, 0 failed"],
          "a green run ends on its count and nothing else")
    check(summary_lines(7, ["x", "y"])
          == ["7 assertions, 2 failed", "  FAILED: x", "  FAILED: y"],
          "and a red one names every check that failed under the count  "
          "<-- pinned defect")

    print("-" * 68)
    for line in summary_lines(passed[0] + len(failed), failed):
        print(line)
    return 1 if failed else 0


def summary_lines(total, failed):
    """The last lines of a self-test run: the count, then what failed."""
    lines = ["%d assertions, %d failed" % (total, len(failed))]
    lines.extend("  FAILED: %s" % item for item in failed)
    return lines


# ----------------------------------------------------------------------- cli

class _Parser(argparse.ArgumentParser):
    """argparse, with a usage error that exits 64 as documented.

    argparse exits 2 on its own, and 2 here means a side could not be read,
    so a mistyped cron entry read as a service that did not answer.
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(64, "%s: error: %s\n" % (self.prog, message))


def _parse(argv):
    ap = _Parser(
        prog="svcdrift.py",
        description="Diff a published feature service against the dataset it "
                    "was published from, and exit non-zero rather than call "
                    "two different schemas the same.",
        epilog="Read-only. Nothing this tool does changes a service, and the "
               "only thing it writes needs --apply. The tokens may come from "
               "the %s and %s environment variables instead of the command "
               "line, where every process on the machine can read them."
               % (TOKEN_ENV, SOURCE_TOKEN_ENV),
    )
    ap.add_argument("--service",
                    help="the published layer, e.g. "
                         "https://gis.county.org/server/rest/services/"
                         "Parcels/FeatureServer/0")
    ap.add_argument("--source",
                    help="what it was published from: another layer url, a "
                         ".json snapshot, or a feature class for arcpy to "
                         "open")
    ap.add_argument("--token", help="a portal token for a secured service")
    ap.add_argument("--source-token", dest="source_token",
                    help="a token for a --source service. Without it, the "
                         "--service token goes to a --source on the same "
                         "host and to no other host")
    ap.add_argument("--out",
                    help="file to write the service's layer definition to, as "
                         "the baseline for the next run")
    ap.add_argument("--timeout", type=float, default=HTTP_TIMEOUT,
                    help="seconds to wait for the service, above 0 and at "
                         "most %d (default %d)" % (MAX_TIMEOUT, HTTP_TIMEOUT))
    ap.add_argument("--probe", action="store_true",
                    help="also query one row and report advertised fields the "
                         "data does not carry")
    ap.add_argument("--strict", action="store_true",
                    help="fail on a warning as well as on a break")
    ap.add_argument("--schema-only", dest="schema_only", action="store_true",
                    help="compare the fields and the layer properties only, "
                         "and leave the renderer, the labels and the "
                         "visibility range out, as version 1.0 did")
    ap.add_argument("--json", action="store_true",
                    help="write the report as json on stdout instead of text")
    ap.add_argument("--apply", action="store_true",
                    help="write the --out file. Without this nothing is "
                         "written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        # A scheduled task on Windows writes to a cp1252 pipe. An alias with a
        # character outside cp1252 raised UnicodeEncodeError after the
        # comparison had finished, which exits 1, the code for a break.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    if not args.service:
        print("error: --service is required. Use --self-test to verify the "
              "tool without a service.", file=sys.stderr)
        return 64
    if not is_http_url(args.service):
        print("error: --service must be an http or https url, got %r. The "
              "service side is always the published one; a geodatabase path "
              "goes in --source." % args.service, file=sys.stderr)
        return 64
    if args.source is not None and not args.source.strip():
        print("error: --source is empty.", file=sys.stderr)
        return 64
    for flag, url in (("--service", args.service), ("--source", args.source)):
        if has_userinfo(url):
            # The url is not echoed: the password is in it.
            print("error: the %s url carries a user name or password, which "
                  "this tool cannot log in with and would print. Give a token "
                  "instead." % flag, file=sys.stderr)
            return 64
    if not args.source and not args.probe and not args.out:
        print("error: nothing to do. Give --source to compare against, "
              "--probe to check the service against its own data, or --out to "
              "save a baseline.", file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out, the file to write the baseline to.",
              file=sys.stderr)
        return 64
    if not 0 < args.timeout <= MAX_TIMEOUT:
        # Written this way round so a NaN, which compares false, is refused.
        print("error: --timeout must be above 0 and at most %d seconds."
              % MAX_TIMEOUT, file=sys.stderr)
        return 64

    token = args.token or os.environ.get(TOKEN_ENV) or None
    source_token = args.source_token or os.environ.get(SOURCE_TOKEN_ENV) \
        or (token if same_host(args.service, args.source) else None)
    # A token pasted into a url is printed in the report otherwise. The url
    # is still read as given, and clean_url() drops its query string.
    service_label = redact(args.service, token, source_token)
    source_label = redact(args.source or "", token, source_token)
    symbology = not args.schema_only
    source = None
    probed = None
    diffs = []
    try:
        published = read_service(args.service, token, args.timeout)
        service = normalize_layer(published, service_label, symbology)
        if args.source:
            read, _kind = read_source(args.source, source_token, args.timeout)
            source = normalize_layer(read, source_label, symbology)
            diffs.extend(diff_layers(source, service, symbology))
        if args.probe:
            features = query_one(args.service, token, args.timeout)
            probed = len(features)
            diffs.extend(probe_fields(service, features))
    except (RuntimeError, ValueError) as exc:
        # ValueError is how the pure core rejects a definition it cannot trust:
        # a field with no name, two fields with one name. Those come from the
        # service, not from a bug here, so they are an exit code and a message
        # rather than a traceback.
        print("error: %s" % redact(exc, token, source_token), file=sys.stderr)
        return 2

    diffs = sort_differences(diffs)
    # fetch_json has taken each side's own token out of its body. This takes
    # both tokens out of everything printed, whichever side carried them.
    if args.json:
        print(redact(json.dumps(build_report(diffs, source_label, service_label,
                                             source, service, args.strict,
                                             probed),
                                indent=2, sort_keys=True),
                     token, source_token))
    else:
        for line in describe(diffs, source_label, service_label, source,
                             service, args.strict, probed):
            print(redact(line, token, source_token))

    if args.out:
        # With --json the report on stdout has to stay parseable, so the note
        # about the baseline goes to stderr instead.
        note = sys.stderr if args.json else sys.stdout
        if not args.apply:
            print("", file=note)
            print("Check only. No baseline was written. Re-run with --apply.",
                  file=note)
        else:
            try:
                path = write_snapshot(snapshot_document(published,
                                                        args.service),
                                      args.out)
            except RuntimeError as exc:
                print("error: %s" % exc, file=sys.stderr)
                return 2
            print("", file=note)
            print("wrote %s" % path, file=note)

    return exit_code(diffs, args.strict)


if __name__ == "__main__":
    sys.exit(main())
