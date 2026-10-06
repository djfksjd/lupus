# Third-party notices

Lupus itself has no runtime dependencies. One third-party file is distributed with it,
unmodified, for the graph viewer (`lupus graph`).

## vis-network 10.1.2

- Upstream: https://github.com/visjs/vis-network (commit f2584c8d1264d8591e6c4d8f09e3a81d599ce1b8)
- File: `src/lupus/viewer/vendor/vis-network.min.js`, copied byte-for-byte from
  `standalone/umd/vis-network.min.js` in the npm package `vis-network@10.1.2`
- Licence: dual-licensed Apache-2.0 OR MIT. Lupus uses it under the MIT licence. Both licence
  texts are kept next to the file (`LICENSE-MIT`, `LICENSE-APACHE-2.0`); the copyright header
  inside the file is intact.
- Copyright (c) 2011-2017 Almende B.V., (c) 2017-2019 visjs contributors
- The standalone bundle includes its own bundled dependencies (vis-data, vis-util, hammer.js,
  keycharm, component-emitter, uuid and core-js polyfills), each under MIT-compatible terms as
  stated by their authors. Lupus did not audit those bundled sources.

Pinned source, tarball integrity (sha512, checked against the npm registry on 2026-10-05) and
per-file sha256 are recorded in `src/lupus/viewer/vendor/upstream-lock.json`; a test fails if
the vendored file no longer matches the lock.

Updating: download the new package tarball, verify its `dist.integrity` against the registry,
replace the three files, update the lock, re-run the tests and re-render the viewer once.
Do not edit the vendored file; changes belong in `viewer.js`.
