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
PASS  latestWkid wins over wkid: a service reports pennsylvania state plane south as Esri's 102729 and EPSG's 2272 together, and only the second one means anything to anybody else  <-- pinned defect
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
...
PASS  but a republish that reverses every key and every class list, drops an alpha, adds float noise, spells defaults out, changes the case of the field and adds authoringInfo is not drift  <-- pinned defect
PASS  a class drawn in another colour is a BREAK: the legend now says something false about every feature in it  <-- pinned defect
PASS  a class that is gone is a BREAK, and names its value  <-- pinned defect
PASS  a layer that stops drawing at a scale where it drew is a BREAK  <-- pinned defect
PASS  a colour ramp whose top stop is now blue is a break: every feature near the top is drawn in a colour the legend gave another value  <-- pinned defect
PASS  a colorInfo and a sizeInfo listed in the other order, with the colour stops reversed too, draw the same map and are not drift  <-- pinned defect
PASS  a visual variable is matched by its type, so a colour ramp moved to another field is a break named as that and not as a position in a list  <-- pinned defect
PASS  a dot density attribute moved to another field is a break, not plain json that exits 0  <-- pinned defect
PASS  a class for 0.3 written back as 0.30000000000000004 is the same class, not one removed and one added  <-- pinned defect
PASS  class breaks listed in another order, with float noise on one and a classMinValue the server wrote out, are the same breaks  <-- pinned defect
PASS  a break that moved from 5 to 6 is one difference, and the symbols are not then compared against a different range  <-- pinned defect
...
PASS  the snapshot this tool writes carries no token  <-- pinned defect
PASS  and a service that advertises a field its data does not carry is caught over the wire  <-- pinned defect
PASS  and the connection error carries no token, although urllib puts the url it could not open into its own message  <-- pinned defect
PASS  a run against a service that drifted exits 1  <-- pinned defect
PASS  --probe on its own fails when the data does not carry an advertised field  <-- pinned defect
PASS  a service whose renderer drifted from another service's exits 1 over the wire  <-- pinned defect
PASS  a service republished with nothing but JSON noise in its drawingInfo exits 0 and prints no differences  <-- pinned defect
PASS  the --service token is not sent to a --source on another host, so a secured source there is refused  <-- pinned defect
PASS  --source-token gives the source host a token of its own
PASS  and --schema-only never reads it, as version 1.0 did not  <-- pinned defect
PASS  an --out that cannot be written exits 2, not 1, which is the code for a break  <-- pinned defect
PASS  a service that could not be read exits 2, not 1: no answer is not the same as no drift  <-- pinned defect
...
PASS  argparse's own usage error exits 64 as documented, not the 2 that means a side could not be read  <-- pinned defect
...
--------------------------------------------------------------------
842 assertions, 0 failed
```

## Requirements

Python 3.9 or newer. Nothing to install and no third-party package.

`arcpy` is optional, and only for the source side. You need it to read a feature class with
`--source`. A service against another service, a service against a saved `.json` snapshot, and
`--self-test` all run on a plain `python3` with no geodatabase on the machine at all. The `arcpy`
import lives inside one function, and the self-test asserts that.

The same 842 assertions pass on Windows (Python 3.13), on Ubuntu (Python 3.12), on ArcGIS Pro's
Python (3.13) and on Python 3.9.

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
python svcdrift.py --service .../Parcels/FeatureServer/0 --source baseline.json --schema-only
```

| Flag | Default | What it does |
|---|---|---|
| `--service` | none | The published layer, with its layer id. Required. |
| `--source` | none | What it was published from: a layer url, a `.json` snapshot, or a feature class. |
| `--token` | none | A portal token for the `--service` host. Env: `SVCDRIFT_TOKEN` |
| `--source-token` | none | A token for a `--source` service. Env: `SVCDRIFT_SOURCE_TOKEN` |
| `--probe` | off | Also query one row and report advertised fields the data does not carry. |
| `--strict` | off | Fail on a warning as well as on a break. |
| `--schema-only` | off | Compare fields and layer properties only. Leave out the renderer, the labels and the visibility range, as version 1.0 did. |
| `--json` | off | Write the report as JSON on stdout instead of text. |
| `--out` | none | File to write the service's layer definition to, as tomorrow's baseline. |
| `--apply` | off | Write the `--out` file. Without it nothing is written. |
| `--timeout` | `60` | Seconds to wait for the service. Above 0 and at most 86400. |
| `--self-test` | off | Run the assertions and exit. |

`--source` is read by what it looks like: an `http` url is another service, a name ending in
`.json` is a snapshot, anything else goes to `arcpy`.

A token goes only to the host it was issued for. A `--source` on the same scheme, host and port
as `--service` is sent the `--token`. A `--source` on any other host is sent `--source-token`, or no
token at all. The `--service` token never goes to a server it was not issued for.

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

## The renderer drifts too

A county's republish batch moved 122 services to a new server. The schema checks passed, the item
ids matched and every service pointed at the right data. A comparison of the renderers, run
afterwards, found 30 of the 122 services drawing differently, in 1,354 differences. A parcel drawn
in the colour of a different status, or placed in the wrong acreage band, is correct data drawn
wrongly on a public map. Nothing fails, so nothing alerts.

When both sides carry a `drawingInfo`, the renderer, its classes, its colour and size ramps, the
label classes and the layer's visibility range are compared as well:

```
$ python svcdrift.py --service http://127.0.0.1:7811/ParcelsDrift/FeatureServer/0 \
      --source http://127.0.0.1:7811/Parcels/FeatureServer/0
svcdrift: source -> service
  source:  http://127.0.0.1:7811/Parcels/FeatureServer/0  (2 field(s))
  service: http://127.0.0.1:7811/ParcelsDrift/FeatureServer/0  (2 field(s))
--------------------------------------------------------------------
BREAK    CLASS_REMOVED            renderer           the value X has a class in the source and none in the service, so those features draw as the default symbol or not at all
BREAK    CLASS_SYMBOL_CHANGED     renderer           class P: symbol.color is [255, 170, 0, 255] in the source, [0, 112, 255, 255] in the service
BREAK    VISIBILITY_NARROWED      scaleRange         visible from the closest zoom out to 1:100000 in the source, from the closest zoom out to 1:50000 in the service, so it stops drawing where it drew
--------------------------------------------------------------------
3 break(s), 0 warning(s)
VERDICT: BREAK
```

The same two fields are on both sides, so a schema check passes this service. With
`--schema-only` the run above prints `no differences` and exits 0, which is what version 1.0 did.

The hard half is what is not reported. A server writes a renderer back differently from the way it
was published: keys in another order, classes in another order, `[168, 0, 0]` for
`[168, 0, 0, 255]`, a width of `0.7000000000000001`, `"angle": 0` spelled out and an `authoringInfo`
block added. The canned `ParcelsNoise` layer holds every one of those, and against `Parcels`
the run prints `no differences` and exits 0.

Both runs are against `python -m http.server` on loopback, serving canned layer definitions.
`http.server` ignores the `?f=json` on the url and serves the file.

## A generic JSON diff does most of this

[DeepDiff](https://github.com/seperman/deepdiff) is the tool to reach for when two JSON documents
have to be compared, and it is good. `ignore_order=True` matches list items whatever order they
are in, and `significant_digits` takes float noise out. It reports every path that differs.

DeepDiff 9.1.0, run with `ignore_order=True, significant_digits=6` on the `drawingInfo` of
`Parcels` and `ParcelsNoise`, reports the noise-only republish as four changes. `authoringInfo`
was added, and each of the three classes changed, because each lost an alpha and gained an angle
of 0. It does not know that a colour with no alpha is the colour with 255, or that a class breaks
renderer is matched by range. It has no opinion about which difference moves a feature into another class
and which moves a label. You can teach it with exclude paths and custom operators. This file is
what that teaching looks like.

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

The renderer, when both sides carry a `drawingInfo`. The rule for the severity: what a feature
MEANS on the map is a break, and how the map looks without meaning anything different is a warning.

| Difference | Severity | Why |
|---|---|---|
| Renderer type changed | BREAK | Every feature is drawn by a different rule. The classes are then not compared. |
| Renderer field, expression or normalization changed | BREAK | Every class now holds other features. The classes are then not compared. |
| Class breaks changed | BREAK | A feature can land in a different class. The class symbols are then not compared. |
| Class removed | BREAK | Those features draw as the default symbol, or not at all. |
| Class symbol changed | BREAK | The legend is how a reader decodes the map, and it is now false for that class. |
| Default symbol removed | BREAK | A feature that matches no class stops drawing. |
| Visibility range narrowed | BREAK | The layer stops drawing at a scale where it drew. |
| Class added | WARNING | Nothing that drew yesterday draws differently. |
| Class label changed | WARNING | The legend text moved. That is all. |
| Simple symbol changed | WARNING | One symbol for every feature encodes no value. |
| Default symbol added or changed | WARNING | |
| Visual variable field, expression or normalization changed | BREAK | The ramp now shows another attribute on every feature. |
| Colour ramp changed | BREAK | A `colorInfo` stop, colour or data range moved, so a value is drawn in a colour the legend gave another value. |
| Visual variable removed | BREAK | The value it showed is gone from the map. |
| Visual variable added | WARNING | Nothing that drew yesterday draws differently. |
| Size, opacity or rotation ramp changed, or a stop's label | WARNING | A larger value is still drawn larger. The legend text moved. |
| Label class added, removed or changed | WARNING | No feature moves. |
| Visibility range widened | WARNING | Nothing that drew stops drawing. |
| Any other renderer or `drawingInfo` property | WARNING | Compared as JSON, so a property this file has never heard of is still compared. |

Unique value classes are matched by value, never by position. Class breaks are matched by range,
after sorting by the upper bound. Label classes are matched by their expression and where clause.
Visual variables are matched by their type and their target, so a `colorInfo` and a `sizeInfo`
listed in the other order are the same two. The attributes of a dot density or pie chart renderer
are classes: a new field is `RENDERER_FIELD_CHANGED`, a new colour is `CLASS_SYMBOL_CHANGED`, and a
lost attribute is `CLASS_REMOVED`, all breaks. They are matched by position, because a pie chart
draws its slices in that order.

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
- **A renderer is compared in one canonical form.** Before two `drawingInfo` objects are compared,
  both lose what a republish adds and a reader cannot see:
  - Key order, and the order of the classes, the label classes and the visual variables.
  - The order of the stops of a visual variable. A stop sits on the ramp at its value. Two stops
    on one value keep their order, because that order is the hard edge they make.
  - Float noise. Two numbers are one number when they differ by no more than 1e-9 of the larger,
    or by no more than 1e-9 below 1: `math.isclose` with both tolerances set to 1e-9. That takes
    out `1234.5600000000001` and nothing a person could type. A class break edited from 1000000 to
    1000001 is a change, whether the server wrote it as `1000000` or `1000000.0`, and so is a
    scale of 1:100000 that became 1:100001.
  - A colour's missing alpha, which is 255, and float noise on a channel, which rounds away. The
    same holds for each colour of a `colorInfo`'s `colors` list.
  - A null, an empty list and an empty object, which say the same as a key that is not there.
  - A key that holds its default, such as `angle`, `xoffset` and `yoffset` of 0, a `transparency`
    of 0, a label `minScale` or `maxScale` of 0, `kerning` true, and an empty `label` or
    `description`.
    A default must match in type too, so a `kerning` of 1 is not read as true.
  - `authoringInfo` and `classificationMethod`, which record how the breaks were chosen. The breaks
    themselves are compared.
  - The `url` of a picture symbol that carries its own `imageData`. The url is the name the server
    filed the image under.
  - The line ending and the edge spaces of an expression. A class value keeps its spaces.
  - The case of the field a renderer reads, as for field names.
  - The spelling of a class value: `1`, `1.0` and `"1"` are one class. So are `"A, R1"` and
    `"A,R1"` for a two-field renderer. A fraction is read to 15 significant digits, so `0.3`
    written back as `0.30000000000000004` is one class. The number tolerance does not apply to
    a class value, because the server matches a class value exactly.
  - A `NaN`, which a JSON reader accepts. Two of them are one value, or a layer that holds one
    never equals its own snapshot.
  - `uniqueValueGroups` when `uniqueValueInfos` is there. A newer server writes the same classes
    twice. A side that has only the groups is read from them.
- **A class breaks renderer that does not say where it starts.** A side with no `minValue` is not a
  side that moved the first break. A `classMinValue` that equals the class below it, which a server
  may write out on every class, is not a difference.

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
| 2 | A side could not be read: the service was unreachable, the snapshot was not json, `arcpy` was not there. Also a `--out` file that could not be written. |
| 64 | Usage error, including a flag `argparse` refuses. |

Exit code 2 is deliberately not 1. A service that did not answer is not a service that matched, and
a nightly job has to be able to tell those apart.

## Limits

- One layer per run. A service with twelve layers takes twelve runs, one per layer id. Pointing
  `--service` at the service root is a refusal that names a layer id to use, not a guess.
- Definitions only. Nothing here compares values, counts rows or checks geometry. A service holding
  the right fields, the right renderer and yesterday's data passes.
- `--probe` reads one row. It finds fields the data never carries. It cannot find a field that is
  null in the row it happened to read, and it says nothing at all about a layer that returned no
  rows, which the report states rather than passing quietly.
- Indexes, editor tracking, attachments and relationship classes are not compared.
- The renderer is compared only when both sides carry a `drawingInfo`: a service against a service,
  or a service against a snapshot of one. A feature class read through `arcpy` has no
  `drawingInfo`, so a feature class against a service compares no symbology at all. Reading a
  renderer out of a Pro map's CIM needs a normalizer of its own, and there is none here.
- The visibility range is the layer's `minScale` and `maxScale`, compared only when both sides
  report one. A snapshot written by version 1.0 holds the whole layer definition, so it carries
  both, and a comparison against it now includes the renderer. Pass `--schema-only` to keep the
  old result.
- A CIM symbol, the part of a visual variable that is not its field or its colour ramp, and any
  renderer property without a rule of its own are compared as JSON, after the canonical form
  above. The report names the first path that differs, not every one. Two visual variables with
  the same type and target are paired in the order of their canonical JSON, as label classes are.
- A size ramp is a warning even when its data range moved, which makes every feature a different
  size. A reader decodes a size by comparison, not by the legend, so the tool does not fail a
  nightly run on it. Pass `--strict` to fail on it.
- Dot density and pie chart attributes are paired by position. Two attributes that swapped places
  read as two changed fields and two changed colours, which they are on a
  pie chart.
- Two label classes with the same expression and where clause are paired in the order of their
  canonical JSON. When two of those both changed, the report can pair them the other way round. It
  still reports a change, not a match.
- A `labelExpression` on one side and the same text as a `labelExpressionInfo` on the other are two
  different label classes. The tool does not translate between the older syntax and Arcade.
- Symbology differences are never grouped. A republish that repainted every class prints one line
  per class, which is the size of what went wrong.
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
  token is never written into it, and never printed: every error message and both urls in the
  report are passed through a redaction that strips the tokens it was given and any `token=` in a
  url. A layer definition is redacted before it is parsed and again after, so a token the server
  echoes back behind a JSON escape, such as `\/` or `\u0053`, is taken out too.
- A token pasted into a `--service` url as `?token=` is not sent. The query string of a layer url
  is dropped before the read, so pass the token with `--token` or `SVCDRIFT_TOKEN`.

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
