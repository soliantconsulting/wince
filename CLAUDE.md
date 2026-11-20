# wince

## Versioning

Every commit that changes what someone running wince gets bumps the version, in the same commit. That covers code, the page, packaging and bundled files; docs, tests and comments alone don't count.

- Before 1.0: a new feature or a behavior change bumps the middle number (0.2.0 → 0.3.0). A fix bumps the last number (0.2.0 → 0.2.1).
- Change `VERSION` in both `wince/core.py` and `wince/app/common.js`. `python3 test_wince.py` fails if they differ.
- After pushing a bump, tag it and push the tag: `git tag v0.3.0 && git push origin v0.3.0`.
- If a change to the page's JS or CSS should reach browsers right away, also bump the `?v=` numbers on the `common.js` and `style.css` links in the HTML pages.

## Checks

Run `python3 test_wince.py` before committing.
