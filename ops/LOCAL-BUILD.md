# Local C++ build verification

Use the maintained checkout and its exact TDLib submodule. The required Ubuntu
packages are `build-essential`, `clang-21`, `cmake`, `ninja-build`, `gperf`,
`libssl-dev`, `zlib1g-dev`, `pkg-config` and `ccache`. Inspect installed packages
first and install only missing dependencies; an unrelated system upgrade is not
part of a build.

Fetch the pinned submodule before starting the offline unit. Create the locked
system user `telegram-bot-api-build` with its private home at
`/var/lib/telegram-bot-api-build`. Populate `source/` there from the maintained
public checkout, preserving the parent/TDLib Git revisions but excluding any
private configuration, credentials, session state or authentication helpers.
The build user owns this separate writable copy; keep the maintained checkout
intact. It needs sufficient scratch space for source, objects and a 2 GiB ccache.

Install `build_local.py` under `/opt/telegram-bot-api/ops/` and the example unit
as `telegram-bot-api-build.service`. The unit is a manual, finite build job with
no boot installation target. It uses one compiler job, one CPU core, low CPU/I/O
priority, 3 GiB memory high/4 GiB maximum, no swap and no external network. These
are ceilings, not a guarantee of spare host capacity. Inspect actual available
memory, swap activity and pressure before admission and while compiling; stop
only this owned build if it causes harmful contention. Keep reusable objects and
cache for a subsequent resume.

The runner records source/submodule/input hashes, compiler/dependency versions,
commands, elapsed time, binary checksum and real-binary offline checks in
`receipt.json`. Full compiler output stays in `build/local-build.log`. It checks
the version, linked dependencies, eight rejected proxy/configuration cases and
two isolated loopback startups. No production bot credentials or state enter
the build, and no provider or deployment action is performed by the runner.
Review the actual receipt before promoting a local artifact. Different compiler
and system-library versions can legitimately produce a different checksum from
the CI artifact; source identity and verification are recorded separately.
