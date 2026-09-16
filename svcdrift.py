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

It is read-only. Nothing it does changes a service, and the only thing it
writes is a snapshot file, which needs --apply.

    python svcdrift.py --self-test
    python svcdrift.py --service https://gis.county.org/server/rest/services/Parcels/FeatureServer/0 \
        --source parcels_baseline.json
    python svcdrift.py --service .../FeatureServer/0 --source C:/data/parcels.gdb/Parcels
    python svcdrift.py --service .../FeatureServer/0 --source .../Other/FeatureServer/0 --strict
    python svcdrift.py --service .../FeatureServer/0 --probe
    python svcdrift.py --service .../FeatureServer/0 --out baseline.json --apply

Exit codes: 0 no breaking difference, 1 a breaking difference (or any
difference under --strict), 2 a side could not be read, 64 usage error.
"""

from __future__ import print_function

import argparse
import collections
import datetime
import io
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# =============================================================================
# CONFIGURATION. Deliberately not constants at the call site.
# =============================================================================

VERSION = "1.0"

# Seconds before a request to a service is abandoned. A layer definition is a
# small document, but an Enterprise server under a cache rebuild can take most
# of a minute to answer the first call of the day.
HTTP_TIMEOUT = 60

# What replaces a token anywhere it could otherwise be printed. urllib puts the
# url it could not open into its own error message, and that url carries the
# token as a query parameter, so every error text goes through redact().
REDACTED = "[redacted]"

# Environment variable the token may arrive in, so that a scheduled task does
# not have to put it on a command line where every process on the box can read
# it. A --token flag is still accepted, because an interactive run is a
# different threat model from a cron entry.
TOKEN_ENV = "SVCDRIFT_TOKEN"

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
        values = domain.get("codedValues") or []
        for cv in values:
            # Every other rejection in this file is a ValueError, which main()
            # turns into an exit code. A coded value that is not an object used
            # to reach cv.get() and come out as an AttributeError traceback.
            if not isinstance(cv, dict):
                raise ValueError("a coded value must be an object, got %r"
                                 % (cv,))
        codes = tuple(sorted(
            "%s=%s" % (cv.get("code"), cv.get("name")) for cv in values))
        return ("codedValue", name, codes)
    if domain.get("range") is not None or kind == "range":
        return ("range", name, tuple(domain.get("range") or ()))
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
    for entry in raw.get("types") or raw.get("subtypes") or []:
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


def normalize_layer(raw, label=""):
    """A layer definition reduced to what is worth comparing.

    A property the side does not report is left out entirely, so that
    comparable() can tell "both sides say point" from "neither side said".
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


def diff_layers(left, right):
    """Every difference between two normalized layers, in report order."""
    return sort_differences(diff_fields(left, right)
                            + diff_layer_properties(left, right))


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
        for name in (feature.get("attributes") or {}):
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

def redact(text, secret=None):
    """Text with any token taken out of it.

    Two ways a token escapes: the caller's own secret appearing verbatim, and a
    url in an error message that carries token= in its query string. urllib
    quotes the url it could not open back into its own exception, so the second
    one happens without anybody writing a print statement.
    """
    out = "%s" % (text,)
    if secret:
        out = out.replace(secret, REDACTED)
    return re.sub(r"(?i)(token=)[^&\s'\"]+", r"\1" + REDACTED, out)


def is_http_url(value):
    """True for something urllib will open over http."""
    if not isinstance(value, str):
        return False
    return value.strip().lower().startswith(("http://", "https://"))


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
    for line in error.get("details") or []:
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


def fetch_json(url, params, token=None, timeout=HTTP_TIMEOUT):
    """GET a url and return the parsed body, or raise RuntimeError.

    Every failure comes back as RuntimeError with a message the token has been
    taken out of, including the failures that arrive as HTTP 200.
    """
    query = dict(params or {})
    if token:
        query["token"] = token
    full = build_url(url, query)
    try:
        with urllib.request.urlopen(full, timeout=timeout) as response:
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(redact("%s answered HTTP %s" % (url, exc.code),
                                  token))
    except urllib.error.URLError as exc:
        raise RuntimeError(redact("%s could not be reached: %s"
                                  % (url, exc.reason), token))
    except OSError as exc:
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
    return payload


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
    features = payload.get("features")
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
    """Write the snapshot and return the path."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
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
    import shutil
    import socket
    import tempfile
    import threading
    import time

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
    check(sr_key({"wkid": 2881}) == ("wkid", 2881),
          "a wkid is read")
    check(sr_key(2881) == ("wkid", 2881), "a bare integer wkid is read")
    check(sr_key({"wkid": 102658, "latestWkid": 2881}) == ("wkid", 2881),
          "latestWkid wins over wkid: a service reports florida state plane "
          "west as Esri's 102658 and EPSG's 2881 together, and only the "
          "second one means anything to anybody else  <-- pinned defect")
    check(sr_key({"wkid": 102658, "latestWkid": 2881})
          != sr_key({"wkid": 102658}),
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
    check(sr_key({"wkid": 2881}) != sr_key({"wkid": 2236}),
          "two different florida state plane zones are different")
    check(sr_key({"wkt": 'PROJCS["x"]'}) == ("wkt", 'PROJCS["x"]'),
          "a wkt is read when there is no wkid")
    check(sr_key({"wkt": 'PROJCS["x"]'}) != sr_key({"wkt": 'PROJCS["y"]'}),
          "two different wkt strings are two different spatial references")
    check(sr_key({"wkt": 'PROJCS["x"]'})
          == sr_key({"wkt": 'PROJCS["x"]\n   '}),
          "whitespace in a wkt is not a difference  <-- pinned defect")
    check(sr_key({"wkid": 2881, "wkt": 'PROJCS["x"]'}) == ("wkid", 2881),
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
    raises(lambda: sr_key([2881]), "a list spatial reference raises")

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
    check(layer_spatial_reference({"spatialReference": {"wkid": 2881}})
          == {"wkid": 2881}, "a top level spatial reference is found")
    check(layer_spatial_reference(
        {"extent": {"xmin": 0, "spatialReference": {"wkid": 2881}}})
        == {"wkid": 2881},
          "and so is the one a published layer keeps inside its extent, which "
          "is the only place a real service puts it  <-- pinned defect")
    check(layer_spatial_reference(
        {"spatialReference": {"wkid": 2236},
         "extent": {"spatialReference": {"wkid": 2881}}}) == {"wkid": 2236},
          "the top level one wins when a layer reports both")
    check(layer_spatial_reference({}) is None,
          "a layer that reports neither has none")
    check(layer_spatial_reference({"extent": "everything"}) is None,
          "and an extent that is not an object has none either")
    check(normalize_layer(
        {"extent": {"spatialReference": {"wkid": 2881}}})["spatialReference"]
        == ("wkid", 2881),
          "so a layer read from a real service is comparable on its "
          "projection  <-- pinned defect")
    check(diff_layer_properties(
        normalize_layer({"extent": {"spatialReference": {"wkid": 2881}}}),
        normalize_layer({"spatialReference": {"wkid": 2236}}))[0].kind
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
        "spatialReference": {"wkid": 2881},
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
        "spatialReference": {"wkid": 2881},
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
                "spatialReference": {"wkid": 2881}, "types": []}
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
    proj = diff_layer_properties(base, layer(spatialReference={"wkid": 2236}))
    check(kinds(proj) == ["SPATIAL_REF_CHANGED"],
          "a layer republished in another projection is reported")
    check(proj[0].severity == BREAK, "and it is a break")
    check("wkid 2881" in proj[0].detail, "with the wkid it used to be in")
    check(diff_layer_properties(
        base, layer(spatialReference={"wkid": 2881, "latestWkid": 2881}))
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
    check(property_text(("wkid", 2881)) == "wkid 2881", "a wkid describes")
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
    work = tempfile.mkdtemp(prefix="svcdrift-selftest-")
    try:
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
                factoryCode = 2881

        shaped = layer_from_arcpy(_Describe(), [
            _Field("OBJECTID", "OID"),
            _Field("OWNER", "String", "Owner Name", 80),
            _Field("STATUS", "String", "Status", 16, "StatusDomain"),
            _Field("ACRES", "Double", "Acres", 8)])
        check(shaped["name"] == "Parcels", "the dataset name is read")
        check(shaped["geometryType"] == "esriGeometryPolygon",
              "arcpy's Polygon becomes esriGeometryPolygon  <-- pinned defect")
        check(shaped["spatialReference"] == {"wkid": 2881},
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
        }
        hits = []

        class _Stub(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                hits.append(self.path)
                path, _sep, raw = self.path.partition("?")
                params = urllib.parse.parse_qs(raw)
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
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
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

            # ---- the token out of the environment, driven for real
            #
            # A scheduled task cannot put a token on a command line, where
            # every process on the box reads it out of the process table. The
            # environment variable is the whole point, so it is run rather
            # than described.
            env_out = os.path.join(work, "secret.json")
            os.environ[TOKEN_ENV] = "set-before-the-self-test-ran"
            held = os.environ.pop(TOKEN_ENV, None)
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
                os.environ[TOKEN_ENV] = held
            check(os.environ.get(TOKEN_ENV) == "set-before-the-self-test-ran",
                  "and the self-test puts the caller's own %s back, rather "
                  "than leaving the process it ran in changed  "
                  "<-- pinned defect" % TOKEN_ENV)
            os.environ.pop(TOKEN_ENV, None)

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
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    finally:
        shutil.rmtree(work, ignore_errors=True)

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

    print("-" * 68)
    total = passed[0] + len(failed)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for item in failed:
            print("  FAILED: %s" % item)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="svcdrift.py",
        description="Diff a published feature service against the dataset it "
                    "was published from, and exit non-zero rather than call "
                    "two different schemas the same.",
        epilog="Read-only. Nothing this tool does changes a service, and the "
               "only thing it writes needs --apply. The token may come from "
               "the %s environment variable instead of the command line, "
               "where every process on the machine can read it." % TOKEN_ENV,
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
    ap.add_argument("--out",
                    help="file to write the service's layer definition to, as "
                         "the baseline for the next run")
    ap.add_argument("--timeout", type=float, default=HTTP_TIMEOUT,
                    help="seconds to wait for the service (default %d)"
                         % HTTP_TIMEOUT)
    ap.add_argument("--probe", action="store_true",
                    help="also query one row and report advertised fields the "
                         "data does not carry")
    ap.add_argument("--strict", action="store_true",
                    help="fail on a warning as well as on a break")
    ap.add_argument("--json", action="store_true",
                    help="write the report as json on stdout instead of text")
    ap.add_argument("--apply", action="store_true",
                    help="write the --out file. Without this nothing is "
                         "written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
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
    if not args.source and not args.probe and not args.out:
        print("error: nothing to do. Give --source to compare against, "
              "--probe to check the service against its own data, or --out to "
              "save a baseline.", file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out, the file to write the baseline to.",
              file=sys.stderr)
        return 64
    if args.timeout <= 0:
        print("error: --timeout must be greater than zero.", file=sys.stderr)
        return 64

    token = args.token or os.environ.get(TOKEN_ENV) or None
    source = None
    probed = None
    diffs = []
    try:
        published = read_service(args.service, token, args.timeout)
        service = normalize_layer(published, args.service)
        if args.source:
            read, _kind = read_source(args.source, token, args.timeout)
            source = normalize_layer(read, args.source)
            diffs.extend(diff_layers(source, service))
        if args.probe:
            features = query_one(args.service, token, args.timeout)
            probed = len(features)
            diffs.extend(probe_fields(service, features))
    except (RuntimeError, ValueError) as exc:
        # ValueError is how the pure core rejects a definition it cannot trust:
        # a field with no name, two fields with one name. Those come from the
        # service, not from a bug here, so they are an exit code and a message
        # rather than a traceback.
        print("error: %s" % redact(exc, token), file=sys.stderr)
        return 2

    diffs = sort_differences(diffs)
    if args.json:
        print(json.dumps(build_report(diffs, args.source or "", args.service,
                                      source, service, args.strict, probed),
                         indent=2, sort_keys=True))
    else:
        for line in describe(diffs, args.source or "", args.service, source,
                             service, args.strict, probed):
            print(line)

    if args.out:
        # With --json the report on stdout has to stay parseable, so the note
        # about the baseline goes to stderr instead.
        note = sys.stderr if args.json else sys.stdout
        if not args.apply:
            print("", file=note)
            print("Check only. No baseline was written. Re-run with --apply.",
                  file=note)
        else:
            path = write_snapshot(snapshot_document(published, args.service),
                                  args.out)
            print("", file=note)
            print("wrote %s" % path, file=note)

    return exit_code(diffs, args.strict)


if __name__ == "__main__":
    sys.exit(main())
