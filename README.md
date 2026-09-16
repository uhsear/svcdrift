# svcdrift

Diff a published feature service against the dataset it was published from, and exit non-zero rather than call two different schemas the same.

Somebody adds a `ZONING` field to the parcel feature class. The nightly load picks it up and writes
it. Every log is green, the row counts are right, and the field is there in the geodatabase the
next morning.

The service never learns about it. A feature service is a copy of the schema taken at publish time,
not a view of it, so the layer definition still lists the six fields it was published with. The
popup does not show `ZONING`. The Experience Builder filter somebody built on `ZONING` matches
nothing and returns an empty map. Nothing failed, so nothing alerted, and the first report is a
phone call from a citizen three weeks later.

```
$ python svcdrift.py --self-test
svcdrift self-test: no arcpy, no portal, a stub server on 127.0.0.1
--------------------------------------------------------------------
PASS  a rest type is already canonical
PASS  arcpy's Float is a single, not a double  <-- pinned defect
PASS  arcpy's OID is esriFieldTypeOID  <-- pinned defect
PASS  a type this file has never heard of is passed through, so a newer server does not read as a schema change  <-- pinned defect
...
PASS  an arcpy domain NAME against the service's full coded value list of the same name is not a change, because only the name is knowable on both sides  <-- pinned defect
PASS  latestWkid wins over wkid: a service reports florida state plane west as Esri's 102658 and EPSG's 2881 together, and only the second one means anything to anybody else  <-- pinned defect
PASS  and so is the one a published layer keeps inside its extent, which is the only place a real service puts it  <-- pinned defect
...
PASS  but the comparison reads only the fields that hold data  <-- pinned defect
PASS  a source whose object id is FID and a service whose object id is OBJECTID have two fields, not four  <-- pinned defect
PASS  so the object id is NOT reported as removed  <-- pinned defect
PASS  and the service's object id is not reported as added  <-- pinned defect
PASS  the same rescue applies to the global id field  <-- pinned defect
...
PASS  and an alias-only change is a WARNING, never a break  <-- pinned defect
PASS  a small integer republished as an integer is reported as a widening, not as a type change  <-- pinned defect
PASS  the two fixtures differ in exactly six ways and all six are found
PASS  and they are the six that were seeded, with no seventh
PASS  and a field the service does not have is a BREAK  <-- pinned defect
PASS  and a field the source does not have is a WARNING  <-- pinned defect
PASS  and every removal becomes an addition  <-- pinned defect
...
PASS  warnings alone exit 0, so a run does not fail on an alias  <-- pinned defect
PASS  --strict makes a warning fail too  <-- pinned defect
PASS  the snapshot this tool writes carries no token  <-- pinned defect
PASS  and a service that advertises a field its data does not carry is caught over the wire  <-- pinned defect
PASS  and the connection error carries no token, although urllib puts the url it could not open into its own message  <-- pinned defect
PASS  a run against a service that drifted exits 1  <-- pinned defect
PASS  --probe on its own fails when the data does not carry an advertised field  <-- pinned defect
PASS  a service that could not be read exits 2, not 1: no answer is not the same as no drift  <-- pinned defect
--------------------------------------------------------------------
509 assertions, 0 failed
```

## Requirements

Python 3.9 or newer. Nothing to install and no third-party package.

`arcpy` is optional, and only for the source side. You need it to read a feature class with
`--source`. A service against another service, a service against a saved `.json` snapshot, and
`--self-test` all run on a plain `python3` with no geodatabase on the machine at all. The `arcpy`
import lives inside one function, and the self-test asserts that.

The same 509 assertions pass on Windows, on Ubuntu, and on ArcGIS Pro's Python.

```
git clone https://github.com/uhsear/svcdrift.git
```

## Quick start

```
python svcdrift.py --self-test
python svcdrift.py --service https://gis.county.org/server/rest/services/Parcels/FeatureServer/0 \
    --out parcels_baseline.json --apply
```

Save the baseline today. Compare against it tomorrow.

## Usage

The `--service` side is always the published layer, named down to its layer id. The `--source` side
is whichever copy of the schema you still have.

```
python svcdrift.py --service .../Parcels/FeatureServer/0 --source parcels_baseline.json
python svcdrift.py --service .../Parcels/FeatureServer/0 --source C:/data/parcels.gdb/Parcels
python svcdrift.py --service .../Parcels/FeatureServer/0 --source .../Staging/FeatureServer/0
python svcdrift.py --service .../Parcels/FeatureServer/0 --probe
python svcdrift.py --service .../Parcels/FeatureServer/0 --source baseline.json --json
```

| Flag | Default | What it does |
|---|---|---|
| `--service` | none | The published layer, with its layer id. Required. |
| `--source` | none | What it was published from: a layer url, a `.json` snapshot, or a feature class. |
| `--token` | none | A portal token. Env: `SVCDRIFT_TOKEN` |
| `--probe` | off | Also query one row and report advertised fields the data does not carry. |
| `--strict` | off | Fail on a warning as well as on a break. |
| `--json` | off | Write the report as JSON on stdout instead of text. |
| `--out` | none | File to write the service's layer definition to, as tomorrow's baseline. |
| `--apply` | off | Write the `--out` file. Without it nothing is written. |
| `--timeout` | `60` | Seconds to wait for the service. |
| `--self-test` | off | Run the assertions and exit. |

`--source` is read by what it looks like: an `http` url is another service, a name ending in
`.json` is a snapshot, anything else goes to `arcpy`.

The tool is read-only. No flag changes a service. The only thing it writes is the `--out` file, and
that needs `--apply`.

## What one run says

A feature class that gained a field, against the service it was published from:

```
$ python svcdrift.py --service http://127.0.0.1:7801/rest/services/Parcels/FeatureServer/0 \
      --source C:/Users/lolzi/gisdemo/parcels.gdb/Parcels
svcdrift: source -> service
  source:  C:/Users/lolzi/gisdemo/parcels.gdb/Parcels  (8 field(s))
  service: http://127.0.0.1:7801/rest/services/Parcels/FeatureServer/0  (6 field(s))
--------------------------------------------------------------------
BREAK    LENGTH_DECREASED         OWNER              length 120 in the source, 80 in the service, so a value that fits the source is truncated
BREAK    DOMAIN_REMOVED           STATUS             the source has the domain StatusDomain and the service has none, so the picklist and the validation are gone
BREAK    FIELD_REMOVED            ZONING             esriFieldTypeString in the source, absent from the service
--------------------------------------------------------------------
3 break(s), 0 warning(s)
VERDICT: BREAK
```

Eight fields on one side and six on the other, and only one of the two extra fields is reported.
The other is `Shape`, which the feature class lists and the service does not. Nor is `OBJECTID`
reported, although `arcpy` calls its type `OID` and the service calls the same type
`esriFieldTypeOID`. What the tool declines to report is the difficult half of this comparison, and
it is set out under **What it refuses to report** below.

`--probe` answers a different question. It asks the layer for one row and compares what came back
against what the layer definition advertises:

```
$ python svcdrift.py --service http://127.0.0.1:7802/rest/services/Parcels/FeatureServer/0 --probe
svcdrift: source -> service
  source:  (none, the service was read on its own)
  service: http://127.0.0.1:7802/rest/services/Parcels/FeatureServer/0  (6 field(s))
--------------------------------------------------------------------
BREAK    FIELD_NOT_RETURNED       ACRES              the layer advertises esriFieldTypeDouble and the query did not return it
BREAK    FIELD_NOT_RETURNED       OWNER              the layer advertises esriFieldTypeString and the query did not return it
--------------------------------------------------------------------
probe: 1 feature(s) read from the service
2 break(s), 0 warning(s)
VERDICT: BREAK
```

That service passes a schema comparison. Its layer definition matches the baseline exactly, field
for field. The data stopped carrying two of the fields it advertises, which is what a service looks
like after the columns were dropped from the underlying table and nobody restarted it. Only a query
finds it.

Both runs above are against [restfake](https://github.com/uhsear/restfake) on loopback, which is how
this tool is tested. `restfake.py --apply --port 7802 --drop-fields OWNER,ACRES` serves a layer that
advertises a schema its own data does not keep.

## What it checks

Field by field:

| Difference | Severity | Why |
|---|---|---|
| Field removed | BREAK | The popup, the filter and the symbology rule that named it all stop. |
| Field added | WARNING | Nothing that worked yesterday stops working. |
| Type changed | BREAK | Every client that parses the value has to change. |
| Type widened | WARNING | A short integer to a long holds every value it held. |
| Length decreased | BREAK | A value that fits the source is truncated. |
| Length increased | WARNING | Nothing stops. |
| Alias changed | WARNING | A label moved. That is all. |
| Domain removed | BREAK | The picklist and the validation are gone. |
| Domain added or changed | WARNING | Editing gains or moves a constraint. |
| Identity field renamed | WARNING | The publish named the object id something else. |

And what the layer is, rather than what it holds: `geometryType` and `spatialReference` are breaks,
because every client that draws the layer stops. A lost capability, such as `Create` disappearing
from an editable layer, is a break. `maxRecordCount` and a subtype code added are warnings. Subtypes
removed is a break, because the editing templates go with them.

With `--probe`, a field the definition advertises and the data does not return is a break, and a
field the data returns and the definition does not list is a warning.

`--strict` makes every warning fail as well. Use it once the noise is gone, not before.

## What it refuses to report

Most of this file is the difference between two schemas. The useful part is the differences it
declines to call differences, because each one fires on **every** comparison and a report that
cries wolf on every run is read by nobody.

- **The object id and the global id are a role, not a name.** The publish decides what they are
  called and the source has no say. A geodatabase whose object id is `FID`, or `OBJECTID_1` after a
  join, against a service that calls it `OBJECTID` is one field, not one removed and one added. The
  tool pairs unmatched identity fields by role and reports the rename as a warning. Two unmatched
  identity fields on one side are left alone, because nothing can tell them apart.
- **The shape is not a field here.** `arcpy.ListFields` returns the geometry field of every feature
  class and a published service often does not list it. What the geometry actually is gets compared
  once, as `geometryType`.
- **`Long` and `esriFieldTypeInteger` are one type.** `arcpy` and the REST API have different names
  for the same thing, and comparing them raw reports a type change on every field of every dataset
  comparison.
- **Only a text field has a length.** `arcpy` reports length 4 for a long and 8 for a date. The REST
  API reports those inconsistently or not at all.
- **A domain `arcpy` knows only the name of.** `Field.domain` is a name and nothing else, while the
  service hands back the whole coded value list. When either side knows only the name, only the
  names are compared.
- **102100 and 3857 are the same projection.** So are `wkid` and `latestWkid`, and so is the same
  WKT wrapped differently.
- **A property one side does not report is not a difference.** A feature class has no
  `maxRecordCount` and no capabilities. Reading those as absent would report every dataset
  comparison as having lost the capabilities of the service.
- **Field order is not a difference.** The report is sorted, so reading either side's fields in a
  different order gives a report that is identical byte for byte.

## Compare Schema does this for geodatabases

ArcGIS Pro's Data Comparison toolset is the tool people reach for, and Compare Schema is good at
what it does. It reports field, index, subtype and domain differences, it writes a report you can
hand to somebody, and `FeatureCompare` will fail a script on a mismatch.

It compares two geodatabases. It has no notion of a published service: there is no REST endpoint it
can open and no layer definition it can read. In a local government a large part of the schema that
matters lives on the other side of that endpoint, in services that Experience Builder apps, Field
Maps forms and public dashboards are all built on top of. That is the gap this fills. Pro compares
what you have; this compares what you published.

The other half is severity. Compare Schema tells you what differs. It has no opinion about which of
those differences breaks a dashboard, so a nightly job built on it either fails on every alias edit
or fails on nothing.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No breaking difference. Warnings may have been printed. |
| 1 | At least one break, or any difference at all under `--strict`. |
| 2 | A side could not be read: the service was unreachable, the snapshot was not json, `arcpy` was not there. |
| 64 | Usage error. |

Exit code 2 is deliberately not 1. A service that did not answer is not a service that matched, and
a nightly job has to be able to tell those apart.

## Limits

- One layer per run. A service with twelve layers takes twelve runs, one per layer id. Pointing
  `--service` at the service root is a refusal that names a layer id to use, not a guess.
- Schema only. Nothing here compares values, counts rows or checks geometry. A service holding the
  right fields and yesterday's data passes.
- `--probe` reads one row. It finds fields the data never carries. It cannot find a field that is
  null in the row it happened to read, and it says nothing at all about a layer that returned no
  rows, which the report states rather than passing quietly.
- Indexes, editor tracking, attachments, relationship classes and the renderer are not compared.
- A polygon feature class carries `Shape_Area` and `Shape_Length` and a point service does not, so
  comparing those two reports both as removed. They are removed. The geometry type difference above
  them in the report is the reason.
- An unknown field type is passed through rather than folded, so a service on a newer release than
  this file does not read as a schema change. Two sides that spell the same unknown type differently
  will read as one.
- Domains are compared by name and coded values. A coded value list whose codes are the same and
  whose descriptions changed reads as a change, because a popup shows the description.
- No repair. This tool decides whether the two sides agree. Republishing the service, or an
  overwrite through the ArcGIS API for Python, is what fixes it.
- It writes nothing without `--apply`, and the only file it writes is the `--out` baseline. The
  token is never written into it, and never printed: every error message is passed through a
  redaction that strips both the token it was given and any `token=` in a url urllib quoted back.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [sightline](https://github.com/uhsear/sightline) - the same services from the viewer's side: what they can actually see
- [restfake](https://github.com/uhsear/restfake) - a fake service to test this against, including a schema that lies
- [fullpull](https://github.com/uhsear/fullpull) - pull the service down once you know its schema is right
