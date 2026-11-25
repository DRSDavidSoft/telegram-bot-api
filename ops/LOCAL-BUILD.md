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

The runner samples host memory and swap every five seconds. It terminates only
its newly created compiler process group if available memory drops below 768 MiB,
or after three samples below 1280 MiB / below 1792 MiB with swap above 2 MiB/s.
These conservative stops preserve compiler logs and reusable objects and are
recorded as a failed resource admission, never as a successful compilation.

The runner records source/submodule/input hashes, compiler/dependency versions,
commands, elapsed time, binary checksum and real-binary offline checks in
`receipt.json`. Full compiler output stays in `build/local-build.log`. It checks
the version, linked dependencies, eight rejected proxy/configuration cases and
two isolated loopback startups. No production bot credentials or state enter
the build, and no provider or deployment action is performed by the runner.
Review the actual receipt before promoting a local artifact. Different compiler
and system-library versions can legitimately produce a different checksum from
the CI artifact; source identity and verification are recorded separately.

## Verified local build

The Office Release build completed on 2026-11-23 at 17:26:50 UTC, using source
`104bca1db1d749c380c30a8e0025bfe2f891e132` and TDLib
`f10611ee1f03f91ddae8185483a1ccfd3d664668`. Clang 21.1.8, CMake 4.2.3 and
Ninja 1.13.2 produced Bot API 10.5 in 7079.317 seconds with one build job.
The 46,342,816-byte executable has SHA-256
`88336207a5f04c3f091eaa135339313f526c1b6f8d00e64022289e6f83e130b1`.

The version and linked-library checks passed, followed by eight configuration
rejection cases and two isolated loopback startups. The source remained clean.
The completed unit exited successfully, with a 1,402,634,240-byte memory peak
and zero swap use. The binary checksum, source and retained completion journal
were independently checked on 2026-11-25. Existing objects and ccache were kept;
no duplicate build was needed. This verification did not activate a production
bot, change an endpoint or include any application credentials.
