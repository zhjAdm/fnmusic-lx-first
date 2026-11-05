# Third-party references and runtime

- Protocol behavior was studied from [javycoder/fnos_music_ext](https://github.com/javycoder/fnos_music_ext) (MIT). This project implements a separate, smaller extension. The `work/upstream` checkout is research material only and is excluded from distribution.
- [XCQ0607/lxserver](https://github.com/XCQ0607/lxserver), Apache-2.0, is downloaded separately as version v2.1.2. Its source and license are available in that repository. Archive SHA256: `1f89639366a7d0f968bfb0e56d88701a60d500a555afd52ae2cc7e5ff4742a63`.
- The runtime downloader adds a documented capability-list patch that exposes only registered platform quality names. Its implementation lives in `scripts/runtime.py`; upstream code remains in the separately downloaded runtime directory.
- lxserver requires music-tag-native 0.2.5 (MIT); missing architecture bindings are fetched from the official npm registry with version and package integrity checks.
- Python dependencies retain their own licenses. `requirements.lock` records the tested dependency set.
- User-provided paid music scripts are private inputs. No rights to redistribute them are granted by this project's license; they are not included in source distributions or wheels.

This project is a community extension and is not an official fnOS application.
