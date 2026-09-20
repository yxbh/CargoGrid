# Geometry reference

The [README](../README.md) covers everyday use. This page is the deeper reference for dimensions, interfaces, print poses and what the checks do.

## Units and datums

All dimensions are millimetres. The southwest corner of the tile body is the XY datum, and Z=0 is the underside. In the Python API and manifest, `Interface.pitch` is the user-facing unit size and `Interface.height` is tile thickness. X-socket centres are at `(pitch/2 + i*pitch, pitch/2 + j*pitch)`.

Let `u` be unit size and `t` be tile thickness. The main tile body is `u*nx` by `u*ny` by `t`. Positive-X/Y male projections and the south female depth are `0.1*u`; the west female depth adds the existing absolute 0.1 mm allowance. Joint widths, necks, walls and 45-degree flanks scale from the standard 60 mm profile. At the standard 60/13 settings, an unterminated tile is `(60*nx+6, 60*ny+6, 13)` and keeps the original 6/6.1 mm depths.

`joint_style="original"` is the default. Male ledges reach `t-3`, and female pocket ceilings reach `t-2.8`; their 0.2 mm difference is an absolute allowance. Tile-facing edge and corner accessories use the same unit size, thickness and joint style as the tile.

The X socket scales in its local plane from the standard 44.556 mm throat. The plug scales from the same nominal shape, while the small plug/socket differences, `fit_offset` and accepted 0.08 mm bracket-stem inset stay absolute. Plug projection is `t-0.2`; the final R2 tip and 3 mm entry roundover also stay physical sizes. When a bracket post is turned upright, the scaled local plane becomes world X/Z and tile thickness becomes its world-Y insertion depth.

The stock-compatible preset is 60 mm units, 13 mm thickness and zero fit offset. Custom parts match other parts made with the same settings; they aren't meant to mix with stock 60/13 parts. Unit size must be at least 30 mm and thickness at least 6 mm. Cell counts, copy counts, build dimensions, packing gaps, the default 10 mm holes, stop heights, the 50 mm ramp run and slicer settings don't scale. Comfort radii also remain physical sizes. The support-rail dovetail is a separate fixed interface and doesn't scale with the tile grid.

## Open-through tile-edge joints

`joint_style="full-height"` is an alternate tile-edge joint. It extends the male tabs to the top and opens the female pockets through the roof. It isn't related to cargo-stop height, tile thickness or printer build height, and its male tabs don't fit original roofed female pockets.

At standard 60/13 dimensions, the negative-X pocket comes very close to the neighbouring X-socket entry. The remaining web is about 0.146 mm thick at Z=12.5 and opens to the outside near Z=12.75, even on a no-hole tile. The CAD can still be a valid solid, but that doesn't make the web strong enough to print or use. This joint style stays experimental.

## Exact footprints and hole scopes

Exact layouts use whole cells at the selected unit size and add the leftover width or depth to terminated edge pieces. Filler placement can be balanced or biased to the positive or negative sides, but it doesn't change unit spacing or interface size. Assembly frames describe the floor; print packing and print poses are separate.

The full 10 mm round-hole pattern is the default. `full` scope uses half-unit grid sites except X-socket centres, including retained edge and corner sites. `interior` keeps only complete interior holes, and `hole_diameter=None` or `--no-holes` makes solid webs. At standard 60 mm units, 1x1, 2x1, 2x3 and 4x4 tiles have 8, 13, 29 and 65 accepted sites. Hole diameter stays 10 mm when unit size changes. Boundaries, fillers and interface keep-outs can reject sites; the manifest records each result. If every site is rejected, try a smaller hole or use `--no-holes`.

Neither enabling holes nor producing one valid solid establishes strength or bridge quality. Check the intended hole centres, retained webs and actual slicer paths for the chosen configuration.

## Non-printing roof modifiers

Roof support is off by default and only works with original-style tile `part` and `layout` jobs. It targets the downward-facing ceilings over retained west and south female pockets. Male edges aren't targeted, and a tile with no eligible female roof is rejected.

`critical` coverage uses two nominal 3 mm bands per roof. It stays at least 6 mm from the roof centre and at least 1 mm beyond an accepted edge-hole radius. `full` keeps the whole roof footprint. A south R5 cutout can split one roof into two faces, so a standard full-hole 2x1 tile has three roofs, six critical modifiers or five full modifiers.

The enforcer half-span is `min(max(1.0, generation_layer_height), roof_thickness)`, giving Z=9.2--11.2 around the default Z=10.2 ceiling. This spans multiple potential layer planes; it is not a permanent 2 mm support slab. Bambu's manual-support calculation selects current-layer regions inside the enforcer and removes regions already covered by the preceding model layer. Changing a profile does not regenerate the masks, so actual contacts still need checking.

Native `support_enforcer` parts aren't tile solids. STEP exports and model packing bounds leave them out, while Bambu print transforms move them with the tile. The slicer can grow support beyond the mask and into west/south round cutouts. On a standard full-hole 2x1 job, support occupies the three cutouts at (0,30), (30,0) and (90,0) while printing. Remove it through the open underside and female edges before assembly.

Logical PETG model/base slot 1 and PLA interface slot 2 are explicit. The maintained dissimilar-material workflow requests `support_top_z_distance=0`, `independent_support_layer_height=0`, `support_interface_spacing=0`, `support_interface_top_layers=2` and `support_on_build_plate_only=0`. These five keys are included in process-override tracking so a profile change does not silently replace the requested contact behavior. At least two dense layers are required for zero contact; explicitly larger counts are preserved. A positive top gap selects a separate gapped request without forcing synchronized layer height.

This zero-gap setup is only for a suitable PETG model/support base with a PLA contact interface. Same-material and unsupported pairs are rejected. If you swap materials in the slicer, revisit the contact settings instead of leaving zero gap on materials that may fuse. The workflow keeps `support_object_xy_distance=0.4`; in the standard 2x1 study, moving from 0.35 to 0.40 mm removed small side-wall overlaps without changing the first-layer footprint. It doesn't change bridge, cooling, speed, prime, flush or automatic-foot settings. Roof support can't be combined with stacks, catalogues, accessories or full-height joints.

### Slice mode and first-layer defaults

Every generated Bambu plate starts in `Auto For Match` / Convenience Mode without a saved physical nozzle map. `--roof-nozzle-slots` switches a roof-support job to Custom mapping. In Bambu's native values, `Auto For Flush` means Filament-Saving Mode and `Manual` means Custom filament grouping. The separate `normal(manual)` support type selects enforcer-defined support and doesn't switch filament grouping.

The zero-contact workflow sets `support_on_build_plate_only` to false. Initial support-foot expansion is left to Bambu unless `--roof-foot-expansion-mm` is supplied; `0` means zero expansion. Automatic nozzle choices can change with the loaded project and filaments, so check them before slicing.

Expansion changes the first support/raft layer, not the model CAD. It may affect bed contact, clearance and removal even when roof-facing interface coverage is unchanged. Neither zero nor automatic expansion is universally correct. Check the actual support and model extrusion footprints, including declared widths and arcs, and distinguish those from nominal CAD sections at the same Z. Layer discretization can make those representations differ.

## Stacks and accessory scope

Stacking adds sacrificial support-base and release-interface volumes between copies of the same tile. The gap must leave some support-base thickness after both interfaces. The manifest records quantities, partial batches and material roles. A valid stack file still doesn't tell you how cleanly the parts will separate.

The accessory catalogue contains independently authored functional edge/corner pieces, X-plug plates, normal full-solid stops, angled stops, vertical tile brackets and physical support rails. It is not a promise that every contour or secondary mechanism matches another design. Physical support rails use separate end-to-end dovetails and a 25 mm supporting depth; they are not X-plug attachments, and no positive mat-to-rail latch is assumed.

Catalogue membership depends on the chosen build envelope and interface settings. Oversized parts are listed as omitted rather than shrunk, and ordered tile sizes stay distinct even when either orientation fits. Bambu uses the per-family poses listed under [Export checks](#export-checks), then packing may add a 90-degree XY turn.

### Vertical tile brackets

`vertical-tile-bracket` replaces the unreleased `lock-90` catalogue family without retaining an alias. It supports three depth-matched configurations—floor 1x2 -> wall 1x2, floor 2x1 -> wall 2x1 and floor 2x2 -> wall 2x2—and two independent-height configurations—floor 1x1 -> wall 1x2 and floor 2x1 -> wall 2x2. API `nx` is base/wall X width, `ny` is floor-base Y depth, and optional `panel_height_cells` is wall Z height; omitting or explicitly matching it to `ny` preserves the original specification and stable ID. Floor and wall posts use the same interface unit/thickness, including the rotated world-X/Z wall profile. Standard 60/13 remains the stock-compatible preset.

The bracket is one full-width, solid-backed wedge. In general, a floor base is `nx*u` wide by `ny*u` deep, and its downward plug tips reach Z=`-(t-0.2)`. At standard 60/13 dimensions, the original bases are 60x120, 120x60 and 120x120 mm, plug tips are Z=-12.8, and the lower body through Z=3.1 is unchanged. There are no outboard tabs, through channels or split parts.

For base depth `d`, the wall-tile seat is Y=`d-t`, the post tips end at Y=`d-0.2`, and wall rows are spaced by `u`. Panel bottom and the bearing plane stay at the fixed physical Z=6.1 mm. A full-width strip from Z=4.1 to 6.1 provides nominal zero-gap bearing. At standard 60/13 dimensions with the illustrated full-hole tiles, planar contact is about 260.892 mm2 on 1x2 and 521.784 mm2 on the two-column brackets. Those figures describe CAD contact, not insertion force or load capacity.

The wall tile remains a separate part. The default placement has its underside facing out. For the top face outward, rotate it Z=180 then X=-90; use that same orientation on every adjoining wall tile because left and right swap. The backing turns covered interior holes into blind pockets while assembled. At standard thickness those pockets are about 13 mm deep; edge cutouts aren't all sealed bores. Same-orientation left, right and upper neighbours keep their mating positions, while a lower neighbour is blocked by the floor bracket.

All five brackets use the accepted two-way wall post. The wide root flare is gone, the straight stem profile is inset 0.08 mm, and a 0.1 mm transition reaches the unchanged R2 tip. For thickness `t`, that transition spans insertion depths `t-2.3` to `t-2.2`; at standard 13 mm thickness it is 10.7--10.8 mm. The post extends 2 mm behind the seat for support. In the standard 1x1-floor to 1x2-wall CAD check, the top-outward tile seats with zero volume collision, while the underside-outward tile keeps 5.439166 mm3 total nominal overlap. A straight top-outward insertion sweep reaches the same overlap as the tip crosses the narrow entry. The post design is accepted, but insertion force and retention still need a print test.

The two shallow variants retain a 60 mm floor depth, panel seat Y=47, panel plug tips Y=59.8 and the original two-row wall Z grid at standard dimensions. Their natural filled rear profile rises 107 mm across the available 42.8 mm upper run rather than forcing the original 45-degree slope. The upper rear body stays behind the connector root while both floor and panel X regions remain exact. All five brackets use R3 at the analogous exposed thick front-to-slope transition; other thick free body edges remain R2, the thin bearing lip remains R1 and the internal panel-bearing corner stays exact. Standard shallow bounds remain 60x60x140.378 mm and 120x60x140.378 mm including downward plugs.

Shallow Bambu exports use Y=-90 degrees broad-side-down and object-scoped normal Auto. At official H2D 0.8/0.32 and 0.4/0.20 PETG settings, both variants had one connected model first layer, complete model-layer schedules and paths inside shared reach. OFF controls emitted floating-cantilever warnings. Auto generated about 6.74/3.93 g support for the one-column variant and 19.91/14.06 g for the two-column variant, reaching both floor and panel X mating regions. Those regions are exposed for access in the side-down pose, but support removal, resulting fit, stability and strength are not physically verified.

### Floor ramps

`ramp` is an independently authored floor-to-mat transition with original roofed tile-edge joins. Its width is an integer number of unit cells, its rise is tile thickness `t`, and its finished body run stays 50 mm in positive Y. API `ramp_join="female"` and CLI `--ramp-join female` are the defaults; omitting the choice preserves existing female design names. Each cell has one female pocket at its half-unit centre, receiving a tile's north male edge at Y=0.

`ramp_join="male"` / `--ramp-join male` uses the same wedge with shared male tile-edge tabs pointing toward negative Y. Tabs project `0.1*u` beyond the joining face, so the source bounds are `nx*u` by `50+0.1*u` by `t`: 60x56x13 mm for a standard one-cell male ramp. This extra tab projection is not taken from the slope's 50 mm run. The manifest's `mating_datums` separates body run, tab projection and overall depth. Only the outward half of each shared male tool is added; its construction wall cannot flatten the slope at larger unit sizes.

For a matching tile whose body begins at (0,0), place a female ramp along the north edge with translation `(0, tile_depth, 0)`, or along the east edge with Z=-90 rotation followed by translation `(tile_width, ramp_width, 0)`. Place a male ramp along the south edge with Z=180 rotation followed by translation `(ramp_width, 0, 0)`, or along the west edge with Z=90 rotation and no translation. These placements start at the tile corner; offset by whole cells for a longer mat. Rotation changes the direction the slope faces, not the joining sex. At standard dimensions the male tabs project 6 mm into the south's 6 mm pocket or the west's 6.1 mm pocket, and the ledge/roof seating gap stays 0.2 mm.

The profile is one filled wedge with a nominal 10 mm carrier before the shelf-to-slope round and a floor-tangent rounded nose. Side and nose rounds are R2, the mating-boundary underside transition is R1, and shared tile-joint tools provide the joins. At standard 60/13 dimensions the female ramp pocket is 6.1 mm deep. Separate ramps meet without overlap, while a multi-cell ramp is one continuous solid. The high joining rim is left unchanged.

The flat carrier meets the slope through a tangent R32 arc. At standard thickness the bend is about 13.246 degrees: it starts at Y=6.284 mm, ends at Y=13.617 mm and spans 7.332 mm horizontally while dropping 0.851 mm. The main slope plane, 50 mm finished body run and 13 mm rise remain unchanged. The broader curve replaces the much subtler R2 shelf blend; it does not round the high joining rim. Since the standard female pocket ends at Y=6.1 mm, its 2.8 mm roof remains intact beneath the flat shelf.

R32 is a physical maximum, not a unit-scaled radius. For slope angle `a`, the resolved radius is `min(32, (10-2)/tan(a/2))`, retaining at least 2 mm of high flat shelf when thicker custom ramps make the slope steeper. For example, a 60 mm-thick ramp uses about R15.330. The manifest records the resolved shelf radius. Custom unit sizes or thicknesses can place some pocket roof beneath the curved top; their local roof thickness is not necessarily the standard 2.8 mm. The shelf and nose arcs are constructed in section before extrusion and side rounding, so shallow slopes cannot silently lose their blend. Finished-surface checks measure radius and tangent continuity at both boundaries across the width. A CAD viewer may still draw those tangent boundaries.

Ramps require original roofed joints. Width and the joins follow unit size, rise follows tile thickness and the finished body run remains the approved physical 50 mm. Fit allowances and comfort radii stay absolute. Experimental full-height ramp geometry remains rejected because a generic post-fillet cutter left real overlap with its matching tile. There are no corner ramps, separate male adapters, X plugs, holes or added mechanisms.

The source orientation already places the underside on Z=0. Bambu exports scope normal Auto support to female ramp objects for their pocket roofs without enabling global support; male ramps do not request object support. Packing uses the actual bounds including male tabs. Bounded native checks of 1-cell and 5-cell female ramps at the documented H2D PETG profiles produced one connected first model layer and continuous model schedules. The deposited first layer reached about 48.72 mm of the 50 mm run at 0.8/0.32 and 48.47 mm at 0.4/0.20; the remaining nose is the upward-curving tangent tip. Auto support occupied only the exposed female-pocket region and must be removed before assembly. This is slicer evidence, not physical fit, adhesion or traffic/load validation.

With the broad R32 shelf, native Bambu Studio 02.08.02.61 checks used the installed H2D 0.8 nozzle / 0.32 mm Balanced Strength / PETG Basic settings on one- and five-cell ramps, plus a one-cell 0.4/0.20 comparison. All model layers were present, and deposited model/support bounds stayed inside common reach; neither brim nor tower was generated. Female ramps retained a connected first model layer and pocket-roof support, with top interface paths at Z=10.12 and Z=10.0 respectively. Male OFF and Auto controls both generated no support, but their first model layer had separate body/tab islands: two for one cell and six for five cells. Auto does not join those islands or establish adhesion. Fresh native project restores retained the embedded settings, model parts, placements, material assignments and object support choices. Factory T-command parser diagnostics remained; these checks did not execute printer commands. Bed adhesion, support removal and printed fit still need a physical trial.

### Normal full-solid vertical stops

`vertical-stop` is a cargo stop, not a wall-tile bracket. The catalogue includes 1x1, 1x2, 2x1 and 2x2 bases at physical shoulder heights H60 and H120 mm. The first count is base X width, the second is base Y depth, and height is measured from the attachment shoulder at Z=0. Unit size changes the base spacing and X profiles; stop height stays 60 or 120 mm. Plug tips are Z=`-(t-0.2)`, or -12.8 mm at standard thickness.

The body is one full-width filled triangular wedge from the 4.1 mm front toe to a solid cargo face at positive Y. It has no open central bay, separate side ribs, wall holes, panel connectors, ledge or tile. The 2x1/H120 geometry is the natural wedge selected for the family rather than an extra-material raised 45-degree toe. A solid CAD body does not request 100% slicer infill.

The retangent profile is solved so the coupled R2 blend reaches the exact requested H60/H120 maximum while the cargo-face Y datum remains exact. One fillet operation rounds all twelve free source edges: cargo cap/perimeter, both diagonal rear boundaries, front/toe perimeter and the complete underside outer perimeter. The protected X plug profiles and existing R1 roots have zero geometric change. R2 on both horizontal boundaries of the 4.1 mm toe leaves a 0.1 mm planar center land at the extreme front; source bodies, STEP roundtrips and closed meshes remain valid.

Each Bambu export computes its X rotation from the actual depth, height and R2 retangent slope so the broad rear face is down before fit checks and packing. Normal Auto support metadata is scoped to the stop object. In the documented H2D PETG native checks, 1x1/H120 and 2x1/H120 generated mounting-region support at both 0.8/0.32 and 0.4/0.20; this requires sliced-path and removal review and does not imply physical print approval. Other profiles can make different support decisions.

### Accessory edge rounds

Original-style edge and corner bodies use R3 before the unchanged tile-joint tools are applied. The asymmetric outer-corner tips are extended only along their free long axis before rounding so their published envelopes remain unchanged. Experimental full-height parts retain their existing selective R2/R1 construction.

Straight rail and connector outer bodies use R3, with R3 window corners and R2 window rims. Rail-end variant 1 uses a coupled R0.75 outer body, R2.5 window corners, R1 horizontal window rims and R1.5 sloped underside rims. Variant 2 uses sequential R3 side caps, R0.25 ramp-profile edges, R2/R2 horizontal windows and R1.5 sloped underside rims. Variants 3/4 use coupled R3 bodies with R3/R2 horizontal windows and R1/R0.75 on their sloped underside window rims. These are the largest tested combinations that keep the exact support dovetails and both default/adaptive STEP-volume budgets on macOS and Linux. Small custom units proportionally cap constrained window-rim radii; the separate support dovetail remains fixed. Attachment plates use R2 around the complete 4.1 mm body, then receive exact X plugs and R2 roots.

Angled `lock-45` stops retain a 4.1 mm front base/toe, a 6 mm horizontal cap thickness (about 4.243 mm normal to the 45-degree cargo face), unchanged outer bounds and exact X connectors/roots. One coupled operation applies R2 to all 18 free envelope edges. The 1x1 and 2x2 Bambu poses remain X=-135 degrees; bounded native 0.8/0.32 and 0.4/0.20 checks produced no support with either OFF or Auto.

The 2x2 angled stop supports unit sizes up to 60 mm because its coupled R2 body fails above that span. The 1x1 version is the supported alternative for larger units.

## Export checks

Each exported design must be one valid, positive-volume solid. STEP reimport checks body count, bounds within 0.00001 mm and adaptive BRepGProp volume at 1e-12 against the existing surface-area-times-OCCT-confusion budget. The writer tries OCCT precision modes against those same gates and keeps the first passing file; it doesn't change the source solid or widen a tolerance. The manifest records the chosen mode, delta, method and budget.

Within one STEP check, the writer measures the unchanged source bounds once and each precision attempt's reimported bounds once, using the same precise CAD query for both. These measurements are local to the check; another export measures again, including after a source shape is moved or changed.

Mesh chord tolerance is 0.02 mm. Surface seams are welded only within 0.000001--0.00001 mm, bounded by kernel vertex tolerance and an independent displacement cap. Unmeshed faces are accepted only below 0.0000000001 mm2; all resulting meshes must still be closed, consistently oriented and positive-volume, without zero-area triangles.

A remaining isolated three-edge crack can be closed only when it is an unambiguous oppositely oriented triangle, contains no duplicate face, has maximum edge 0.1 mm, and has area below both 0.0000001 mm2 and its perimeter times OCCT linear confusion. Fifteen barycentric probes, including vertices and edge-quarter points, must lie within OCCT's 0.0000001 mm tolerance of the unchanged CAD surface. No vertices move. Larger, ambiguous, nontriangular or off-surface gaps remain errors. The mesh report records any repair and the strict checks still run afterward.

Manifests record the generator version, resolved parameters, compatibility notes and unsupported combinations. Core 3MF keeps model orientation and quantities. Bambu-compatible 3MF adds plate, material and modifier metadata. Either file is still unsliced unless you slice it yourself.

Bambu single-part and catalogue exports rotate attachment plates X=180 degrees body-down, original vertical tile brackets by their retangented rear-face-down X angle, shallow brackets Y=-90 degrees (broad side down), normal stops by their per-design broad-rear-face-down angle and angled stops X=-135 degrees (back face down) before bounds checks and packing. STEP/STL and core 3MF retain model orientation. Recommendations include per-artifact applied flags; Bambu plate items include the exact source-to-project transform, composed with their in-plane packing rotation and translation. Normal-stop and shallow-bracket items carry object-scoped `enable_support=1` and `support_type=normal(auto)` without changing global tile/original-bracket support behavior. The pose is already baked into the Bambu mesh and must not be applied twice. API catalogue callers use `orient_for_bambu=True` for the matching eligibility calculation. Roof-support and stack workflows keep their existing orientation and cannot be combined with independently oriented models.

The H2D dual-safe catalogue is a standard 60/13 machine-specific plan, not a generic build rectangle. Common plates use X25..325, Y0..320 and Z<=320, add a 5 mm model inset and keep model bounds at least 10 mm apart. The 306x306 mm 5x5 tile gets its own left-nozzle-only plate because it doesn't fit the 300 mm common width. Slot 1 is mapped left there; common plates keep automatic Convenience Mode. Packing checks model bounds only, so inspect support, brim and tower paths after slicing.

Catalogue fit checks measure each generated candidate in the requested source or Bambu pose once. The H2D planner reuses those precise sizes while grouping and packing the same unchanged designs. Sizes are tied to design identity, not names, and live only for that request; later calls measure again so changed shapes or poses cannot reuse stale bounds.

## Optional reference comparison

Portable tests inspect generated solids, dimensions, layout placement, accepted hole locations, STEP reimports, mesh topology, accessory families, separator contacts and 3MF structure without a redistributed reference mesh.

The optional local comparison explicitly builds original-style geometry and resolves an explicitly supplied local 3MF's per-model IDs and component transforms. It samples socket rays, directional joining sections, mixed original/new signed intervals and insertion offsets. Near-horizontal mesh intersections use a disclosed 0.0001 mm section adjustment. Its 0.008 mm measurement budget covers tessellation and near-tangent sampling, not a manufacturing allowance.

Sampling is not an exhaustive insertion/collision sweep or full-surface equivalence proof. Rounded upper joining patches may differ even when sampled functional datums match. The reference itself can have both interference and clearance; that is not evidence of intended insertion force, calibration or load capacity. Full-height geometry is never reported as an original-compatible pass.

## What still needs a real print

CAD and slicer checks don't measure insertion force, retention, support release, flatness, heat creep, impact strength or load capacity. Record real results with the exact model revision, material and print profile. No load or restraint rating is supplied.
