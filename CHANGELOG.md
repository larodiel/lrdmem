# Changelog

All notable changes to lrdmem. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
versions follow [Semantic Versioning](https://semver.org/). Releases before 0.3.0 are listed on the
[GitHub releases page](https://github.com/larodiel/lrdmem/releases).

## 0.3.0 - 2026-11-09

### Added

- `lrdmem visualize`: opens the whole store as an interactive 3D map in your browser, so you can see how memories
  connect without walking the folders. It regenerates the page from the store on every run.
  - Colour is the environment, shape is the memory type (semantic, procedural, episodic, hypothesis, prospective,
    metacognitive), size is confidence and use.
  - State shows on the node: dashed for candidate, an amber rim for contested, hollow for superseded or obsolete.
  - Each kind of relation has its own line colour (`related_to`, `derived_from`, `occurred_in`, `supports`,
    `contradicts`, `caused_by`, `resolved_by`, `generalizes`, `supersedes`, plus evidence); proposed relations are
    dashed. Memories with no relation sit on the outer shell.
  - Search lights up matches; clicking a node shows its description, confidence, experiences and neighbours.
  - A legend lists what your store actually contains.
  - One offline HTML file: it loads nothing from the network. `-o FILE` picks where it is written and `--no-open`
    skips the browser. Without `-o` it writes a private (0600) temporary file.
- A demo of the map in the README and on the website, built from an invented store.

### Changed

- `lrdmem graph --format html` now writes the same 3D page instead of the old flat one.
- Memory nodes in `lrdmem graph --format json` carry a `description`.
