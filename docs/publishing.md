# Publishing the documentation

The guide is published at <https://datamindzio.github.io/ha-energy-compass/>.

GitHub **Settings → Pages → Build and deployment** uses **Deploy from a branch**, with branch **main** and folder **/docs**. GitHub's built-in Pages workflow publishes updates after changes reach that branch. No custom domain, site generator or local build is required. The empty `.nojekyll` file preserves the static HTML as written.

`index.html` is the site entry point. `guide.en.html` and `guide.pl.html` are generated from `guide.en.md` and `guide.pl.md` by `python tools/build_guides.py` (needs the `docs` extra and Node.js for Mermaid). The script pre-renders every diagram to light and dark SVGs in `assets/diagrams/` and reuses the stylesheet from `index.html`, so the site stays static and script-free. Edit the Markdown, rerun the script and commit its output; Pages serves `.md` files only as raw text. Its section links and image paths work both at the project-site URL and from a local checkout. Links to supporting Markdown documents, source files and the license point to their rendered GitHub pages.

`assets/icon@2x.png` and `assets/dark_icon@2x.png` are exact copies of the corresponding files in `custom_components/energy_compass/brand/`. When those originals change, update these copies in the same PR so both themes remain consistent.

Before publishing, validate the HTML, check section links, and preview both themes. After merging, check the **pages build and deployment** workflow and verify the live page and both logo URLs return successfully.
