# BINventory — Design Attribution

Every feature and significant design choice in the project, with who originated it.

BINventory was built through a human-directed, AI-assisted workflow: the owner
set the product, its constraints and acceptance criteria, and validated every
phase on real hardware; Claude (Anthropic) did most of the implementation and
proposed much of the mechanism. This record exists because, in that kind of
workflow, "who decided what" is worth stating precisely rather than leaving
implied. §1–§9 cover the original build; §10 covers the seven-phase
re-architecture and plugin platform; §11 covers release preparation.

## Legend

| Code | Meaning |
|------|---------|
| **AI-auto** | Claude designed and introduced it without being asked |
| **AI-story** | User described a problem, need, or symptom; Claude designed the solution |
| **User-func** | User specified *what* it should do, in functional detail |
| **User-tech** | User specified *how* it should be built (implementation choices) |
| **Joint** | Worked out in discussion — proposal and refinements went back and forth; the user ruled on the final behaviour |

Where a row is split (e.g. **User-func → AI-tech**), the user defined the
behaviour and Claude designed the mechanism to achieve it.

---

## 0. Scope and context

Four clarifications that frame everything below. Several items elsewhere in this
document could otherwise read as oversights when they were deliberate scope
decisions.

### Single-user, locally hosted by design
This is a **locally hosted, single-user application**. Multi-user access,
authentication, concurrency, and horizontal scaling were **not considered**
because they are out of scope, not because they were missed. Anything in this
project that would fail under multi-user load is outside the intended envelope.
The same applies to authentication: the app assumes one trusted operator on a
private LAN.

### Testing model
Automated tests verify logic and contracts: a 232-check API regression suite, a 48-check MCP contract suite
(run against a copy of the live library, or a seeded fresh database) and a
static frontend scan, both run in CI. **Real validation happens on the user's
own hardware after each phase, against real-world data** — a phone, real
photos, real WLED strips and matrix panels — with test data kept separate from
the live library. EasyOCR is stubbed in the automated suite, so the user's runs
are its real-world verification. The Docker image is built and booted by CI
rather than on the user's machine.

### Tagging quality is unassessed, intentionally
**No formal assessment of tagging accuracy or recall has been performed.** Core
features are still being built and validated; optimisation and quality tuning
come afterward. Claims anywhere in this project about dictionary coverage,
match quality, or recognition accuracy are therefore *design intent and
reasoning*, not measured results. The vocabulary tooling (dictionaries as files,
Vocabulary Review, learned words) exists to *enable* that tuning later, not
because tuning has already happened.

### Framework choice was ratified, not defaulted
The user did not specify an implementation framework because **the initial
AI-drafted stack and deployment were judged sufficient for a proof of
concept**. Every row below marked *AI-auto* for stack or deployment therefore
reflects a deliberate acceptance by the user rather than an absence of opinion —
the choices were reviewed and found adequate for the project's current stage.

---

## 1. Core concept and product shape

| Feature / Choice | Attribution | Notes |
|---|---|---|
| Whole product concept — photograph items into labeled bins, search later, get pointed to the right bin | **User-func** | Opening request laid out bins, photo capture, inventory view, edit/move, smart search, phone access |
| "Smart search" as the headline feature ("type screwdriver, see all screwdrivers") | **User-func** | Explicitly called out as the biggest feature |
| Local WebUI accessible from any phone | **User-tech** | Stated as a hard requirement up front |
| Local ML rather than cloud AI | **User-tech** | "I do not want AI for photo recognition… something similar to CLIP from Immich" |
| CLIP as the recognition engine | **User-tech** | User named CLIP specifically |
| Choice of `ViT-B-32` / OpenAI weights specifically | **AI-auto** | User said "like CLIP"; Claude picked the lightest practical variant |
| Flask + SQLite + single-file SPA stack | **AI-auto** (user-ratified) | Chosen for zero-config deployment; accepted as sufficient for a proof of concept. Split into a layered package in the re-architecture (§10) |
| No frontend framework (vanilla JS, one HTML file) | **AI-auto** (user-ratified) | Avoids a build step. Later split into native ES modules, still with no build step (§10) |
| Single-user / no auth / no multi-user scaling | **User-func** | Deliberate scope decision — locally hosted, one operator |
| Dark theme, mobile-first UI, bottom-sheet modal pattern | **AI-auto** | No visual direction was given |

## 2. Recognition and tagging

| Feature / Choice | Attribution | Notes |
|---|---|---|
| Candidate-label tagging approach (score image against a word list) | **AI-auto** | Initial 80-word list |
| Configurable dictionary with comment/uncomment category toggles | **User-tech** | User specified the comment/uncomment mechanism and a custom category |
| Category sizes (200 words per household area, 100 per hardware area) | **User-func** | User set the counts and the category list |
| Electronics-shop dictionary (~316 terms) | **User-func** | User specified the domain and content areas |
| Dictionaries moved out to editable `dictionaries/*.txt` files | **User-func** | User asked for a folder of easily editable files |
| Disable a category by renaming to `.off` | **AI-auto** | Mechanism chosen to satisfy "easy to edit" |
| Hot-reload dictionaries without restart | **AI-auto** | Added alongside the file move |
| Async background tagging queue | **AI-story** | User reported 45s blocking requests and asked for batching |
| Auto-tag ON/OFF toggle + "Tag All" batch run | **User-func** | User asked for a toggle to defer processing |
| Scheduled overnight tagging at a set time | **User-func** | User asked for scheduling |
| Load model on demand, unload when idle | **User-func** | User asked to drop the model when not in use |
| Progress banner with "N of M" and ETA | **User-func** | User specified both the count and the time estimate |
| Rolling-average ETA calculation | **AI-auto** | Method chosen to implement the requested estimate |
| **Label-embedding caching (the major perf fix)** | **AI-story** | User asked whether a bigger dictionary would cost compute; Claude found the real bug — the entire dictionary was re-encoded per photo |
| Hash-keyed on-disk cache for label embeddings | **AI-auto** | Extension of the above, so restarts skip re-encoding |
| Learned vocabulary from manual renames | **AI-auto** | Proposed unprompted as an alternative to a bigger static dictionary |
| Vocabulary Review tool (frequency map, threshold, select/deselect, add or export) | **User-func** | User specified the entire flow in detail, including preselection behaviour |
| Stopword/token filtering in the analyzer | **AI-auto** | Implementation detail |

## 3. OCR

| Feature / Choice | Attribution | Notes |
|---|---|---|
| Adding OCR to the pipeline at all | **AI-story** | User asked whether CLIP reads text in images; Claude explained it can't and proposed OCR |
| Decision to build it | **User-func** | User approved and specified requirements |
| "Manage the OCR tool the same way CLIP is managed" | **User-tech** | Explicit architectural instruction |
| Per-item OCR toggle on Add Item | **User-func** | Specified |
| OCR text becomes the label when no user-defined name | **User-func** | Specified as priority rule |
| Setting: OCR default on/off for new items | **User-func** | Specified |
| Setting: include OCR strings as tags, truncated to 255 | **User-func** | Specified, including the limit |
| Setting: truncation toggle + re-run OCR against the database | **User-func** | Specified |
| EasyOCR as the engine | **AI-auto** | Chosen over Tesseract to avoid a separate Windows binary |
| Confidence threshold (0.35) for discarding noise | **AI-auto** | Implementation detail |
| Selective model loading (OCR-only runs don't load CLIP) | **AI-auto** | Refinement discovered while implementing |

## 4. Search

| Feature / Choice | Attribution | Notes |
|---|---|---|
| Semantic search via CLIP embeddings | **User-func** | Core requested feature |
| Keyword fallback when the model isn't loaded | **AI-auto** | Consequence of the unload-when-idle design |
| Numpy vectorization of similarity scoring | **AI-auto** | Found during a requested code review |
| ✨ button to explicitly load CLIP for a query | **User-func** | User specified a button at the end of the search bar |
| 60-second model keep-alive after a search | **User-tech** | User specified the duration and the behaviour |
| Configurable keep-alive timeout setting | **User-func** | Specified |
| Keep-alive also applied after tagging jobs | **User-func** | User asked for this explicitly if not already present |
| Reaper thread implementation of keep-alive | **AI-auto** | Mechanism |
| Search mode indicator (`✨ smart` / `🔤 keyword`) | **AI-auto** | Added for transparency about which path ran |
| **Photo / reverse-image search** | **User-func** | User specified a photo button on the search page |
| Separate similarity threshold for image-vs-image (0.55 vs 0.18) | **AI-auto** | Image scores run much higher; needed tuning |
| "Find matching items" duplicate check on Add Item | **User-func** | Specified |
| Match card actions: details / add to this bin / find bin | **User-func** | All three specified explicitly |

## 5. Organization and item lifecycle

| Feature / Choice | Attribution | Notes |
|---|---|---|
| Bins with name, location, color | **User-func** | From the opening request |
| Move items between bins | **User-func** | From the opening request |
| Checkout system (virtual "Checked Out" bin) | **User-func** | User specified the pinned pseudo-bin, count display, and that Find still works |
| Implementing "Checked Out" as a synthetic bin rather than a real row | **AI-auto** | Mechanism |
| `origin_bin_id` to preserve home location | **AI-auto** | Mechanism enabling the requested Find behaviour |
| Consume → shopping list | **User-func** | Specified, including retaining photos/tags/descriptions |
| Restock: same bin/photo, or update photo/tags/bin | **User-func** | Both paths specified |
| Restock lighting the destination bin | **User-func** | Specified |
| Secure bin deletion (type "delete" + choose contents disposition) | **User-func** | User specified the confirmation phrase and the two content options |
| Server-side enforcement of the confirmation | **AI-auto** | So safety isn't only cosmetic |
| Re-parenting children instead of orphaning on delete | **AI-auto** | Edge case found while building nesting |
| **Nested bins (bin inside a bin)** | **User-func** | User specified nesting, parent showing child contents, and both unpack options |
| Sub-bin name shown on item cards | **User-func** | User specified it, referencing the search page treatment |
| Bin-name banner at the *top* of cards in parent views | **User-func** | Later refinement, specified |
| Grouping parent-view items by container | **AI-auto** | Added alongside the banner |
| Cycle guards (can't nest a bin into its own descendant) | **AI-auto** | Correctness requirement |
| Bin search bar | **User-func** | Specified |
| Top-level vs all-bins search scope toggle | **User-func** | Specified |
| `allBins` vs `bins` split in the client | **AI-auto** | Bug fix — nested bins were missing from lookups |

## 6. WLED lighting

| Feature / Choice | Attribution | Notes |
|---|---|---|
| WLED integration overall | **User-func** | User gave a five-point functional spec |
| Per-bin IP, LED zone, color, brightness | **User-func** | Specified |
| Jog up/down the strand, resize the zone | **User-func** | Specified |
| Timed illumination or slow pulse | **User-func** | Specified |
| Saving WLED config to the bin; Find buttons on bin and item pages | **User-func** | Specified |
| Live LED strip visualizer in the setup sheet | **AI-auto** | Not requested |
| Ping/device-info to auto-read LED count | **AI-auto** | Not requested |
| **Chase/comet effect** — configurable size, starts away, races toward the bin, brightens on approach, configurable speed and color | **User-func** | Fully specified by the user. Removed in the re-architecture (§10) |
| Server-side frame-by-frame animation to achieve it | **AI-tech** | WLED's native chase can't target a specific bin; a single payload can't express motion |
| Per-IP generation counter to prevent overlapping chases | **AI-auto** | Collision handling |
| `find-item` route resolving checked-out items to their origin bin | **AI-auto** | Needed so Find keeps working for checked-out items |

## 7. Data, correctness, and infrastructure

| Feature / Choice | Attribution | Notes |
|---|---|---|
| SQLite schema and all table design | **AI-auto** | |
| Auto-migration on startup (`ALTER TABLE` for missing columns) | **AI-story** | Built after a schema change silently broke the app |
| `label_source` provenance column | **AI-auto** | Introduced for the planned barcode feature |
| EXIF orientation auto-correction | **AI-story** | User reported photos rotating 90°; Claude diagnosed and designed the fix |
| Manual rotate controls (before and after saving) | **AI-auto** | Fallback for photos with no EXIF |
| `fix_orientation.py` repair script for old photos | **AI-auto** | Not requested. Now `maintenance.py fix-orientation` |
| Storing embeddings as JSON text | **AI-auto** | Simplest workable choice; flagged as a scaling limit. Replaced by float32 BLOBs (§10) |
| DB index on `status` | **AI-auto** | From a requested code review |
| Dead-code removal (`cosine()`, unused blocks, `math` import) | **AI-story** | User asked for an efficiency/dead-code review |

## 8. Deployment and packaging

| Feature / Choice | Attribution | Notes |
|---|---|---|
| Docker as the packaging approach | **AI-auto** (user-ratified) | Recommended when the user asked "what's the best way to package this?"; accepted as sufficient for the proof of concept |
| Multi-stage build, CPU-only PyTorch | **AI-auto** | Image-size optimization |
| Named volumes for photos, DB, model caches | **AI-auto** | |
| `BINVENTORY_DB_DIR` env var | **AI-auto** | Fixed a data-loss bug where the DB lived in the container layer |
| Bind-mounting `dictionaries/` | **AI-auto** | So the files stay host-editable |
| Configurable `MEM_LIMIT` | **AI-auto** | Raised after OCR made 2 GB insufficient |
| Both local and Docker deployment paths documented | **User-func** | User asked for both fully covered in the README |
| Requiring Python 3.11+ on Windows | **AI-story** | Diagnosed from the user's greenlet build failure |

## 9. Documentation

| Document | Attribution | Notes |
|---|---|---|
| `README.md` | **User-func** | User asked for both deployment methods fully outlined |
| `ARCHITECTURE.md` | **User-func → AI-auto** | User asked for thoroughly commented files to learn from; Claude proposed a walkthrough document as more useful than inline comments |
| Integration plan (voice, barcode) | **User-func** | User asked for both integrations to be planned. Kept in the internal design log |
| Handoff and state documents for new context windows | **User-func** | User asked for them, so long-running work could resume in a fresh AI session without loss. Kept in the internal design log |
| Brainstorm brief, refactor kickoff and backlog | **User-func → AI-auto** | User asked for a brainstorm and a refactor plan; Claude structured them as a decision log with IDs (`C4`, `E5`, `G6`) that code comments still reference |
| This attribution table | **User-func** | Specified, including the four categories |

## 10. Re-architecture and plugin platform (phases 0–7)

| Feature / Choice | Attribution | Notes |
|---|---|---|
| Restructure the monolith (2,554-line `app.py`, 3,903-line `index.html`) | **User-func** | User asked for the refactor and the full feature backlog to be planned together |
| Phased delivery — 7 phases, each runnable and hardware-tested before the next | **AI-auto** (user-ratified) | Recommended over a single drop so failures stay bisectable; user agreed |
| "Plugin gate": name three real plugins before building a plugin API | **AI-auto → User-func** | Claude set the test; the user named MCP server, shopping-list restocking and barcode/QR |
| Layered package (`api/` → `services/` → `ml/`, `analysis/`) with an application factory | **AI-auto** (user-ratified) | |
| HTTP-free services layer | **AI-auto** | Driven by the user's MCP-server goal |
| Native ES modules, no build step | **AI-auto** (user-ratified) | Preserves the zero-toolchain property the user valued |
| Hand-rolled versioned migrations over Alembic; forward-only, backup as rollback | **AI-auto** (user-ratified) | |
| Expand/contract data moves (write both → backfill → drop after hardware sign-off) | **AI-auto** | Caught 62 rows that existed only in the legacy embedding format |
| `quantity` column semantics (nullable; counts consumables, not screws) | **User-func** | User defined the meaning and that it stays unwired for now |
| WLED 2D matrix support brought into scope | **User-func** | User had live strip and matrix hardware to test against |
| WLED device registry; matrix addressing with no serpentine maths | **AI-auto** | |
| Removing the chase effect | **AI-auto** (user-confirmed) | Never proven on hardware and wrong on matrices |
| Analyzer pipeline — "analyzers propose, the worker arbitrates" | **AI-auto** | Extracted from the existing CLIP + OCR pair |
| Label-source precedence ranks | **Joint** | |
| Region-select OCR and its rulings (replaces text, sticky against bulk re-runs) | **Joint** | Prompted by real packaging noise winning item names; user ruled on six behaviours |
| Configurable search thresholds, vocabulary management UI, pagination | **Joint** | |
| Maintenance CLI (dry run unless `--apply`; orphans moved, never deleted) | **AI-auto** | |
| Plugin model: in-process, fail-soft, declared permissions, reversible registration, declarative UI | **AI-auto** | |
| Barcode/QR as the first plugin rather than a core feature | **AI-auto** (user-ratified) | Proving ground for the plugin API |
| Online barcode lookup off by default, code only, never photos | **User-tech** | Follows the user's no-cloud constraint |
| Scanned names outranking whole-photo OCR | **AI-story** | Found when a re-run put OCR noise back over a looked-up product name |
| Electronics dictionary left enabled despite the spec | **User-func** | The user's own library depends on it |
| Slow bin loads diagnosed as full-size photos served as thumbnails | **AI-story** | User reported 20–30 s loads; Claude ruled out the model and measured payloads |
| Deferring thumbnails to the final build step | **User-func** | |
| Hardware acceptance script at the end of every phase | **User-func** | The user's working agreement: features are tested on real hardware as they land |
| Regression harness, frontend static scan, broken-plugin fixtures | **AI-auto** | Rebuilt after the original sandbox harness was lost |

## 11. Release preparation

| Feature / Choice | Attribution | Notes |
|---|---|---|
| Publishing on GitHub; trimming public docs to README, ARCHITECTURE and this file | **User-func** | Internal design log kept locally, git-ignored |
| Docker fixes: copy the full package, all state on volumes, TLS SANs for the host IP, healthcheck that works over HTTPS | **AI-story** | User asked for the Docker deployment to be updated; the gaps were found in review |
| Ignore rules covering pre-migration DB copies and the TLS key | **AI-auto** | Found in review — `*.db` alone would have published personal backups |
| Seeded fresh-database test mode and CI (tests + Docker build/boot) | **AI-auto** | So the suite runs on a fresh clone, not only on the owner's library |

---

## 12. Thumbnails, name priority and the MCP server

| Feature / Choice | Attribution | Notes |
|---|---|---|
| Thumbnails as the fix for slow bin loads | **AI-story** (user-approved) | Diagnosis from §10; the user had approved building it as the final step |
| Self-healing thumbnail cache (rebuilt when older than the photo; no migration; immutable versioned URLs) | **AI-auto** | |
| Name priority configurable in Settings, any order, default with the assistant below typed names | **User-func** | User asked for a user-assignable ranking with that default |
| A distinct `agent` name source for the assistant | **Joint** | Claude proposed options; the user chose a separate rank below typed names |
| MCP server as an in-process plugin on its own port | **AI-auto** (user-ratified) | Recommended over a separate stdio process, which would have broken the analysis queue and events |
| This computer only by default; other devices selectable, always with a token | **Joint** | User ruled between the options Claude laid out |
| v1 scope: reads and non-destructive writes, no delete tools | **Joint** | User ruled |
| Target clients: Claude Code and custom MCP clients (no stdio shim) | **User-func** | |
| Tool surface designed for the model: 19 intent-level tools, location strings, bins by name, actionable errors, annotations | **AI-auto** | |
| Asking which bin when a restock's home was deleted (elicitation) | **AI-auto** | |
| Prompt-injection threat model; activity log; per-call permission checks against token scopes | **AI-auto** | |
| Finishing the services layer (item, bin, search logic out of routes) with typed errors | **AI-auto** | Predicted as an MCP prerequisite in the refactor kickoff |
| Plugin API additions: services, app context, settings hook, status rows, show-once values | **AI-auto** | Generic, so future plugins get them too |
| Semantic search also returning exact-word matches | **AI-story** | Found by the MCP tests: while CLIP was warm, a just-added, unanalysed item was unfindable even by its name |
| Live cleanup of a real bin, comparing assistant vision with CLIP and OCR | **Joint** | User asked for the evaluation; Claude ran it through the plugin's own tools and reported both the results and the gaps they exposed |
| Weighted keyword ranking (field × match quality; name matches lead semantic results) | **AI-story** (user-approved) | Found by the evaluation: "ram" listed macramé first |
| `set_item_tags` for the assistant | **User-func** | The evaluation showed renamed items still carried wrong visual-guess tags; the user chose a tool over automatic re-tagging |
| In-app chat with voice (Whisper on the server, changes apply and show as cards), deferred | **Joint** | User set the scope and made the calls; the choice of model is open |
| Restocking plugin moved to the backlog (breaks local-only); the G7 list read as possibilities, not requirements | **User-func** | |
| QR label plugin for the Dymo LabelWriter 4XL, 4×6 and 1×1 in plus common sizes; PDF first, direct print deferred | **User-func** | User named the hardware, sizes and phasing |
| Code style as a setting (QR default / AprilTag / Code 128) with a read-distance guide on the page | **User-func** | Claude derived the per-size estimates and their rationale |
| Plugins tab with each plugin's settings nested under it | **User-func** | |
| Plugin routes, isolated plugin pages in a frame, `[[links]]` into the item sheet and bin view | **Joint** | User asked for plugin web routes; Claude designed the per-plugin routing table (blueprints can't be unregistered) and the framed page |
| Barcodes as local ids at Add Item, conflict prompt, kept separate from product lookup for a later bulk lookup | **User-func** | Claude placed codes in core so a printed label outlives any plugin, and made the conflict check run before anything is saved |
| Scan tab, USB scanners via keyboard focus, optional check in/out on scan with Undo | **AI-auto** | Delivering the user's "scan in or out" |
| Upper-case label URLs for a smaller QR; AprilTags for bins only (587 ids) | **AI-auto** | |
| Address ranking for QR links (home ranges before a VPN's 10.x) | **AI-story** | Found by the live smoke test on the user's machine, not by unit tests |
| AR highlight, shelf-photo map, LiDAR room map, RFID, chat moved to the backlog | **User-func** | |
| Replacement labels (reprint with the same code, or issue a new one), camera scanner overlay, collapsible read-distance guide, centred label text with a text-size setting | **User-func** | Claude designed the reprint memory and the frame-cropping scanner |
| Product lookup sources chosen per use (parts databases for parts), with keys never sent to the browser | **User-func** | Claude picked the candidate sources and added the `secret` setting type |
| Emoji replaced by an icon set; three suites offered, user chose Lucide with its graphite and cyan theme | **Joint** | Claude shortlisted and mocked up Lucide, Phosphor and Tabler; the user chose |
| Code 128 hidden until switched on, with the scanner warning kept on that switch alone | **User-func** | |

## Summary

For the original build (§1–§9):

| Category | Approximate share | Character of the contribution |
|---|---|---|
| **User-func** | ~45% | Nearly every user-facing feature: bins, checkout, shopping list, WLED behaviour including the comet, OCR rules, nesting, search modes, photo search, vocabulary review, deletion safety |
| **AI-auto** | ~40% | Stack, schema, UI design, and a large body of mechanism, edge-case handling, and bug fixes the user never had to specify |
| **AI-story** | ~10% | Cases where a reported symptom or question led to a designed solution: the tagging queue, embedding cache, EXIF fix, migrations |
| **User-tech** | ~5% | A small number of decisive technical calls: local-only, CLIP, "manage OCR like CLIP", comment-toggle dictionaries, 60s keep-alive |

The pattern across the project: the user consistently owned *what the product
does*, defined its scope, and made a handful of high-leverage technical calls,
while Claude owned *how it works* — architecture, mechanisms, edge cases, and
correctness. The most valuable AI contributions were arguably not features at
all but discovered problems: the per-photo dictionary re-encoding, the Docker
data-loss bug, and the EXIF orientation issue.

The re-architecture (§10) kept that division but shifted its weight. The user's
contribution moved from specifying features to **setting gates and ruling on
behaviour**: naming the plugins that justified an API, deciding scope, defining
semantics, and accepting each phase on real hardware. That kind of steering is
what keeps a long, AI-implemented refactor on course.

Two caveats on reading this table:

1. **AI-auto does not mean unreviewed.** Stack and deployment choices were
   accepted by the user as sufficient for a proof of concept. They are
   ratified decisions, not defaults that slipped through.
2. **Nothing here is a quality claim.** Tagging accuracy has not been assessed,
   and real-world validation happens on the user's machine rather than in the
   development sandbox. This table records *who designed what*, not how well
   any of it performs in practice.
