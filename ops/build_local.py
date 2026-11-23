#!/usr/bin/env python3
"""Build the pinned checkout locally and record offline binary verification."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import time


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def output(args, cwd=None, env=None):
    return subprocess.check_output(args, cwd=cwd, env=env, text=True, stderr=subprocess.STDOUT).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--build', required=True, type=Path)
    parser.add_argument('--cache', required=True, type=Path)
    parser.add_argument('--receipt', required=True, type=Path)
    parser.add_argument('--cc', default='clang-21')
    parser.add_argument('--cxx', default='clang++-21')
    args = parser.parse_args()
    source, build, cache = (p.resolve() for p in (args.source, args.build, args.cache))
    if build == source or build.is_relative_to(source) or not (source / 'td').is_dir():
        parser.error('Use a separate build directory and an initialized TDLib checkout')
    if shutil.disk_usage(build.parent).free < 10 * 1024**3:
        parser.error('At least 10 GiB free scratch space is required')
    for path in (build, cache, args.receipt.parent):
        path.mkdir(parents=True, exist_ok=True)
    # Carry no provider credentials, authentication caches or proxy settings into
    # the compiler/test processes. All dependencies must already be available.
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR') if key in os.environ}
    env.update(CCACHE_DIR=str(cache), CCACHE_MAXSIZE='2G', CMAKE_BUILD_PARALLEL_LEVEL='1', GIT_TERMINAL_PROMPT='0')
    if output(['git', 'status', '--porcelain'], source, env):
        parser.error('Reconcile tracked/untracked source changes before building')
    started = time.monotonic()
    project = re.search(r'project\(TelegramBotApi VERSION ([0-9.]+)', (source / 'CMakeLists.txt').read_text())
    if project is None:
        parser.error('Cannot identify the maintained Bot API version')
    expected_version = project.group(1)
    receipt = {
        'started_at_utc': now(), 'status': 'running',
        'source_commit': output(['git', 'rev-parse', 'HEAD'], source, env),
        'td_commit': output(['git', 'rev-parse', 'HEAD'], source / 'td', env),
        'cpp_tree': output(['git', 'rev-parse', 'HEAD:telegram-bot-api'], source, env),
        'cmake_blob': output(['git', 'rev-parse', 'HEAD:CMakeLists.txt'], source, env),
        'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'c_compiler': output([args.cc, '--version'], env=env).splitlines()[0],
        'cpp_compiler': output([args.cxx, '--version'], env=env).splitlines()[0],
        'cmake': output(['cmake', '--version'], env=env).splitlines()[0],
        'ninja': output(['ninja', '--version'], env=env),
        'openssl': output(['pkg-config', '--modversion', 'openssl'], env=env),
        'zlib': output(['pkg-config', '--modversion', 'zlib'], env=env),
        'parallel_jobs': 1, 'build_directory': str(build),
        'expected_version': expected_version,
    }

    def save():
        temporary = args.receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps(receipt, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(args.receipt)

    def run_monitored(command, log):
        # Each command has its own process group. Stop only this owned group if
        # the shared host loses headroom; never signal unrelated processes.
        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        previous_swap = None
        previous_time = time.monotonic()
        pressure_samples = 0
        last_save = 0.0
        try:
            while process.poll() is None:
                stamp = time.monotonic()
                memory = {key: int(value.split()[0]) for key, value in
                          (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}
                vm = dict(line.split() for line in Path('/proc/vmstat').read_text().splitlines())
                swap = int(vm['pswpin']) + int(vm['pswpout'])
                rate = 0 if previous_swap is None else max(0, swap - previous_swap) * os.sysconf('SC_PAGE_SIZE') / 1024 / max(0.001, stamp - previous_time)
                available = memory['MemAvailable']
                monitor = receipt.setdefault('host_monitor', {'minimum_available_kib': available, 'maximum_swap_kib_s': 0, 'samples': 0})
                monitor.update(minimum_available_kib=min(monitor['minimum_available_kib'], available),
                               maximum_swap_kib_s=round(max(monitor['maximum_swap_kib_s'], rate), 1),
                               samples=monitor['samples'] + 1)
                pressured = available < 1280 * 1024 or (available < 1792 * 1024 and rate > 2048)
                pressure_samples = pressure_samples + 1 if pressured else 0
                if available < 768 * 1024 or pressure_samples >= 3:
                    receipt['resource_stop'] = {'available_kib': available, 'swap_kib_s': round(rate, 1)}
                    raise RuntimeError('Owned compilation stopped for host memory/swap pressure')
                if stamp - last_save >= 30:
                    save()
                    last_save = stamp
                previous_swap, previous_time = swap, stamp
                time.sleep(5)
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)

    log_path = build / 'local-build.log'
    commands = [
        ('configure', ['cmake', '-S', str(source), '-B', str(build), '-G', 'Ninja',
                       '-DCMAKE_BUILD_TYPE=Release', '-DCMAKE_C_COMPILER=' + args.cc,
                       '-DCMAKE_CXX_COMPILER=' + args.cxx, '-DCMAKE_CXX_COMPILER_LAUNCHER=ccache',
                       '-DTELEGRAM_BOT_API_ENABLE_LTO=OFF']),
        ('compile', ['cmake', '--build', str(build), '--target', 'telegram-bot-api', '--parallel', '1']),
    ]
    receipt['commands'] = [command for _, command in commands]
    save()
    try:
        with log_path.open('a', buffering=1) as log:
            for stage, command in commands:
                receipt['stage'] = stage
                save()
                log.write('\n' + now() + ' ' + stage + '\n')
                run_monitored(command, log)
            binary = build / 'telegram-bot-api'
            version = output([str(binary), '--version'], env=env)
            if 'Bot API ' + expected_version not in version:
                raise RuntimeError('Unexpected built version')
            dependencies = output(['ldd', str(binary)], env=env)
            if 'not found' in dependencies:
                raise RuntimeError('Missing binary dependency')
            receipt['stage'] = 'offline-binary-checks'
            save()
            result = output(['python3', str(source / 'tests/proxy_smoke.py'), str(binary)], env=env)
            if output(['git', 'diff', '--name-only'], source, env) or output(['git', 'diff', '--name-only'], source / 'td', env):
                raise RuntimeError('Build modified tracked source')
            receipt.update(status='passed', stage='complete', version=version,
                           binary=str(binary), binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
                           binary_bytes=binary.stat().st_size, dependencies=dependencies.splitlines(),
                           offline_checks=result, tracked_source_unchanged=True)
    except Exception as error:
        receipt.update(status='failed', error_type=type(error).__name__)
        raise
    finally:
        receipt.update(finished_at_utc=now(), elapsed_seconds=round(time.monotonic() - started, 3), log=str(log_path))
        save()
    print(json.dumps({key: receipt[key] for key in ('status', 'source_commit', 'td_commit', 'binary_sha256', 'elapsed_seconds', 'offline_checks')}))


if __name__ == '__main__':
    main()
