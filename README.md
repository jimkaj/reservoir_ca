# California Reservoir Storage from Space

How much water is in California's reservoirs? This project estimates it from satellite images.
It measures how much of each reservoir's surface is covered by water, then turns that area into
a volume of stored water.

**See the results:** https://jimkaj.github.io/reservoir_ca/

The website shows a statewide total and a page for each of 38 major reservoirs, from Shasta and
Oroville down to Perris and Pyramid Lake. Each page charts the reservoir's estimated storage over
the last two years, next to the official figures for comparison.

## Why do this from space?

California already publishes official reservoir storage figures, through the state's
[California Data Exchange Center (CDEC)](https://cdec.water.ca.gov/). This project doesn't
replace that. It's an independent way to measure the same thing, using only freely available
satellite imagery. A method like this could be used where no gauge exists, in denied areas, or 
to check one that has gone quiet or started reporting stuck values. Both of those happen in CDEC's 
own data from time to time.

## How it works

**1. Measure the water's surface area.** Two European Space Agency satellite missions photograph
California every few days:

- **Sentinel-1** is a radar satellite. It sends out microwaves and records what bounces back.
  Calm water acts like a mirror and reflects the signal away, so water shows up dark. Radar sees
  through cloud and works at night, so it rarely misses a pass.
- **Sentinel-2** takes ordinary optical pictures, much like a very high-altitude camera. It
  separates water from land well, but it can't see through clouds, and haze or thin high cloud
  can fool it.

For every new image, the project counts the water pixels inside an outline drawn around each
reservoir. The result is a surface area. All of the image processing runs on Google Earth
Engine, so no imagery is downloaded.

**2. Tune the radar against the optical images.** How dark "dark" has to be before radar calls
something water varies from reservoir to reservoir. On days when both satellites passed over,
the project compares them and picks, for each reservoir, the radar setting that best matches the
optical measurement. It then checks that setting on days that weren't used to choose it.

**3. Turn area into volume.** A reservoir is a bowl, so a given surface area corresponds to a
given amount of water. The shape of that relationship differs for every reservoir. The project
learns each one by lining up two years of its own area measurements with the storage CDEC
reported on the same days, then fitting a curve that always rises (more area never means less
water).

**4. Catch bad readings.** A satellite reading that disagrees sharply (by more than 20%) with
the other readings from the same fortnight is flagged as rejected. It's kept, not deleted, but
it isn't used. When both satellites observed a reservoir on the same day, the optical reading is
preferred.

## How accurate is it?

Estimates are checked against CDEC's reported storage:

- **Per reservoir:** the typical (median) estimate is within about **3%** of CDEC.
- **Statewide total:** typically within about **3%** of CDEC's total. On 95% of days it's within
  about 7.5%.
- **On data the curves had never seen:** in a test where the curves were built from the first
  year only and then scored on the second year, the typical error was about **5%**.

There's one important caveat. Because the area-to-volume curves are built from CDEC's own
figures, agreeing with CDEC is partly by design. The comparison is a meaningful check on
measurements taken *after* a curve was fitted, and the website says which comparisons those are.
It is not proof that the method would be this accurate at a reservoir with no official record at
all.

Radar and optical estimates of the same day can differ by several percent, so the statewide
total wobbles a little as new images arrive. The website smooths the radar readings slightly to
reduce this.

## What's left out, and why

The project started with 48 reservoirs. Ten aren't shown, because the satellite measurements
couldn't be made reliable enough there:

| Reservoir | Why it's excluded |
|---|---|
| Hetch Hetchy | Deep, narrow granite canyon. Shadows from the canyon walls confuse the optical images. |
| Loon Lake, Ice House, Cherry Valley, Donner Lake, Independence Lake | High-Sierra lakes that freeze in winter. Even with winter months left out, radar and optical measurements disagreed too much. |
| Success Dam (Lake Success) | Shallow, muddy, and often drawn very low, so the measurements are noisy. |
| Castaic, Terminus (Lake Kaweah), Tulloch | Radar and optical measurements disagreed by more than 15%, and no setting fixed it. |

Some reservoirs that are shown still need special handling:

- A few freeze in winter (Union Valley, Stampede, Donnells), so their winter images are skipped.
- A few read reliably only from certain satellite passes, so the others are ignored.

## Limitations

- **Two years of history.** The record starts in September 2024. The curves only know the water
  levels seen in that time, and a reservoir that drops below its lowest recorded level can't be
  estimated well until the curve is refitted.
- **Radar has seasonal quirks.** In late summer and autumn, radar readings tend to run a few
  percent high compared with the optical ones. Wind on the water also makes radar less reliable.
- **Small reservoirs are harder.** A small reservoir covers fewer pixels, so each misclassified
  pixel along the shoreline matters more.
- **Not official data.** For water supply or safety decisions, use CDEC and the reservoir
  operators.

## Data sources and credits

- Satellite imagery: Copernicus Sentinel-1 and Sentinel-2 (European Space Agency), accessed
  through [Google Earth Engine](https://earthengine.google.com/). The website's images contain
  modified Copernicus Sentinel data.
- Reservoir storage: [California Data Exchange Center (CDEC)](https://cdec.water.ca.gov/),
  California Department of Water Resources.
- Reservoir outlines: [OpenStreetMap](https://www.openstreetmap.org/) contributors.
- Reference land-cover data used for calibration checks: Google's Dynamic World.

## Running it yourself

The code is Python and uses [uv](https://docs.astral.sh/uv/) to manage dependencies. You'll need
a Google Earth Engine account and a Google Cloud project registered for Earth Engine.

```bash
uv sync                                    # install dependencies
uv run earthengine authenticate            # once per machine
uv run python run_pipeline.py --project <your-gcp-project> --dry-run   # see what's due
uv run python run_pipeline.py --project <your-gcp-project>             # measure, convert, build site
```

The built site is written to `./site`. [CLAUDE.md](CLAUDE.md) describes the pipeline in more
technical detail.
