#!/usr/bin/env python3
# appenv - a single file 'application in venv bootstrapping and updating
#          mechanism for python-based (CLI) applications

# Assumptions:
#
#   - the appenv file is placed in a repo with the name of the application
#   - the name of the application/file is an entrypoint XXX
#   - python3.X+ with ensurepip
#   - a requirements.txt file next to the appenv file

# TODO
#
# - provide a `clone` meta command to create a new project based on this one
#   maybe use an entry point to allow further initialisation of the clone.

import argparse
import glob
import hashlib
import http.client
import os
import os.path
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv


def cmd(c, merge_stderr=True, quiet=False):
    # TODO revisit the cmd() architecture w/ python 3
    # XXX better IO management for interactive output and seeing original errors
    # and output at appropriate places ...
    try:
        kwargs = {"shell": True}
        if merge_stderr:
            kwargs["stderr"] = stderr = subprocess.STDOUT
        return subprocess.check_output([c], **kwargs)
    except subprocess.CalledProcessError as e:
        print("{} returned with exit code {}".format(c, e.returncode))
        print(e.output.decode("ascii"))
        raise ValueError(e.output.decode("ascii"))


def get(host, path, f):
    conn = http.client.HTTPSConnection(host)
    conn.request("GET", path)
    r1 = conn.getresponse()
    assert r1.status == 200, (r1.status, host, path, r1.read()[:100])
    chunk = r1.read(16 * 1024)
    while chunk:
        f.write(chunk)
        chunk = r1.read(16 * 1024)
    conn.close()


def ensure_venv(target):
    if os.path.exists(os.path.join(target, "bin", "pip3")):
        # XXX Support probing the target whether it works properly and rebuild
        # if necessary
        return

    if os.path.exists(target):
        print("Deleting unclean target)")
        cmd("rm -rf {target}".format(target=target))

    version = sys.version.split()[0]
    python_maj_min = ".".join(str(x) for x in sys.version_info[:2])
    print("Creating venv ...")
    venv.create(target, with_pip=False)

    try:
        # This is trying to detect whether we're on a proper Python stdlib
        # or on a broken Debian. See various StackOverflow questions about
        # this.
        import distutils.util
        import ensurepip
    except ImportError:
        # Okay, lets repair this, if we can. May need privilege escalation
        # at some point.
        # We could do: apt-get -y -q install python3-distutils python3-venv
        # on some systems but it requires root and is specific to Debian.
        # I decided to go a more sledge hammer route.

        # XXX we can speed this up by storing this in ~/.appenv/overlay instead
        # of doing the download for every venv we manage
        print("Activating broken distutils/ensurepip stdlib workaround ...")

        tmp_base = tempfile.mkdtemp()
        try:
            download = os.path.join(tmp_base, "download.tar.gz")
            with open(download, mode="wb") as f:
                get("www.python.org",
                    "/ftp/python/{v}/Python-{v}.tgz".format(v=version), f)

            cmd("tar xf {} -C {}".format(download, tmp_base))

            assert os.path.exists(
                os.path.join(tmp_base, "Python-{}".format(version)))
            for module in ["ensurepip", "distutils"]:
                print(module)
                shutil.copytree(
                    os.path.join(tmp_base, "Python-{}".format(version), "Lib",
                                 module),
                    os.path.join(target, "lib",
                                 "python{}.{}".format(*sys.version_info[:2]),
                                 "site-packages", module))

            # (always) prepend the site packages so we can actually have a fixed
            # distutils installation.
            site_packages = os.path.abspath(
                os.path.join(target, "lib", "python" + python_maj_min,
                             "site-packages"))
            with open(os.path.join(site_packages, "batou.pth"), "w") as f:
                f.write("import sys; sys.path.insert(0, '{}')\n".format(
                    site_packages))

        finally:
            shutil.rmtree(tmp_base)

    print("Ensuring pip ...")
    cmd("{target}/bin/python -m ensurepip --default-pip".format(target=target))
    if python_maj_min == "3.4":
        # Last version of Pip supporting Python 3.4
        cmd('{target}/bin/python -m pip install --upgrade "pip<19.2"'.format(
            target=target))
    else:
        cmd("{target}/bin/python -m pip install --upgrade pip".format(
            target=target))


def update_lockfile(argv, meta_args):
    print("Updating lockfile")
    tmpdir = os.path.join(meta_args.appenvdir, "updatelock")
    ensure_venv(tmpdir)
    print("Installing packages ...")
    cmd("{tmpdir}/bin/python -m pip install -r requirements.txt".format(
        tmpdir=tmpdir))

    # Hack because we might not have pkg_resources, but the venv should
    tmp_paths = cmd(
        "{tmpdir}/bin/python -c 'import sys; print(\"\\n\".join(sys.path))'".
        format(tmpdir=tmpdir),
        merge_stderr=False).decode(sys.getfilesystemencoding())
    for line in tmp_paths.splitlines():
        line = line.strip()
        if not line:
            continue
        sys.path.append(line)
    import pkg_resources

    result = cmd("{tmpdir}/bin/python -m pip freeze".format(tmpdir=tmpdir),
                 merge_stderr=False).decode('ascii')
    pinned_versions = {}
    for line in result.splitlines():
        spec = list(pkg_resources.parse_requirements(line))[0]
        pinned_versions[spec.project_name] = spec
    requested_versions = {}
    with open('requirements.txt') as f:
        for line in f.readlines():
            spec = list(pkg_resources.parse_requirements(line))[0]
            requested_versions[spec.project_name] = spec

    final_versions = {}
    for spec in requested_versions.values():
        # Pick versions with URLs to ensure we don't get the screwed up
        # results from pip freeze.
        if spec.url:
            final_versions[spec.project_name] = spec
    for spec in pinned_versions.values():
        # Ignore versions we already picked
        if spec.project_name in final_versions:
            continue
        final_versions[spec.project_name] = spec
    lines = [str(spec) for spec in final_versions.values()]
    lines.sort()
    with open(os.path.join(meta_args.base, "requirements.lock"), "w") as f:
        f.write('\n'.join(lines))
        f.write('\n')
    cmd("rm -rf {tmpdir}".format(tmpdir=tmpdir))


def _prepare(meta_args):
    # copy used requirements.txt into the target directory so we can use that
    # to check later
    # - when to clean up old versions? keep like one or two old revisions?
    # - enumerate the revisions and just copy the requirements.txt, check
    #   for ones that are clean or rebuild if necessary
    if meta_args.unclean or not os.path.exists("requirements.lock"):
        print("Running unclean installation from requirements.txt")
        env_dir = os.path.join(meta_args.appenvdir, "unclean")
        ensure_venv(env_dir)
        print("Ensuring unclean install ...")
        cmd("{env_dir}/bin/python -m pip install -r requirements.txt --upgrade"
            .format(env_dir=env_dir))
    else:
        hash_content = []
        requirements = open("requirements.lock", "rb").read()
        hash_content.append(os.fsencode(os.path.realpath(sys.executable)))
        hash_content.append(requirements)
        hash_content.append(open(__file__, "rb").read())
        env_hash = hashlib.new("sha256",
                               b"".join(hash_content)).hexdigest()[:8]
        env_dir = os.path.join(meta_args.appenvdir, env_hash)

        whitelist = set(
            [env_dir, os.path.join(meta_args.appenvdir, "unclean")])
        for path in glob.glob(
                "{meta_args.appenvdir}/*".format(meta_args=meta_args)):
            if not path in whitelist:
                print("Removing expired path: {path} ...".format(path=path))
                if not os.path.isdir(path):
                    os.unlink(path)
                else:
                    shutil.rmtree(path)
        if os.path.exists(env_dir):
            # check whether the existing environment is OK, it might be nice
            # to rebuild in a separate place if necessary to avoid interruptions
            # to running services, but that isn't what we're using it for at the
            # moment
            try:
                if not os.path.exists(
                        "{env_dir}/appenv.ready".format(env_dir=env_dir)):
                    raise Exception()
            except Exception:
                print("Existing envdir not consistent, deleting")
                cmd("rm -rf {env_dir}".format(env_dir=env_dir))

        if not os.path.exists(env_dir):
            ensure_venv(env_dir)

            with open(os.path.join(env_dir, "requirements.lock"), "wb") as f:
                f.write(requirements)

            print("Installing {meta_args.appname} ...".format(
                meta_args=meta_args))
            cmd("{env_dir}/bin/python -m pip install --no-deps -r {env_dir}/requirements.lock"
                .format(env_dir=env_dir))

            cmd("{env_dir}/bin/python -m pip check".format(env_dir=env_dir))

            with open(os.path.join(env_dir, "appenv.ready"), "w") as f:
                f.write("Ready or not, here I come, you can't hide\n")

    return env_dir


def run_app(argv, meta_args):
    os.chdir(meta_args.base)
    env_dir = _prepare(meta_args)
    # Allow called programs to find out where the wrapper lives
    os.environ["APPENV_BASEDIR"] = meta_args.base
    os.execv(os.path.join(env_dir, "bin", meta_args.appname), argv)


def python(argv, meta_args):
    meta_args.command = "python"
    run(argv, meta_args)


def run(argv, meta_args):
    os.chdir(meta_args.base)
    env_dir = _prepare(meta_args)
    cmd = os.path.join(env_dir, "bin", meta_args.command)
    argv[0] = cmd
    os.environ["APPENV_BASEDIR"] = meta_args.base
    os.execv(cmd, argv)


def reset(argv, meta_args):
    print("Resetting ALL application environments in {appenvdir} ...".format(
        appenvdir=meta_args.appenvdir))
    cmd("rm -rf {appenvdir}".format(appenvdir=meta_args.appenvdir))


def init(argv, meta_args):
    print("Let's create a new appenv project.\n")
    command = None
    while not command:
        command = input("What should the command be named? ").strip()
    dependency = input(
        "What is the main dependency as found on PyPI? [{}] ".format(
            command)).strip()
    if not dependency:
        dependency = command
    workdir = os.getcwd()
    default_target = os.path.join(workdir, command)
    target = input(
        "Where should we create this? [{}] ".format(default_target)).strip()
    if target:
        target = os.path.join(workdir, target)
    else:
        target = default_target
    target = os.path.abspath(target)
    if not os.path.exists(target):
        os.makedirs(target)
    print()
    print("Creating appenv setup in {} ...".format(target))
    with open(__file__, "rb") as bootstrap_file:
        bootstrap_data = bootstrap_file.read()
    os.chdir(target)
    with open(command, "wb") as new_command_file:
        new_command_file.write(bootstrap_data)
    os.chmod(command, 0o755)
    with open("requirements.txt", "w") as requirements_txt:
        requirements_txt.write(dependency + "\n")
    print()
    print("Done. You can now `cd {}` and call `./{}` to bootstrap and run it.".
          format(os.path.relpath(target, workdir), command))


def ensure_newest_python():
    if "APPENV_NEWEST_PYTHON" in os.environ:
        # Don't do this twice to avoid surprised with
        # accidental infinite loops.
        return
    import shutil

    current_python = os.path.realpath(sys.executable)
    for version in reversed(range(4, 20)):
        python = shutil.which("python3.{}".format(version))
        if not python:
            # not a usable python
            continue
        python = os.path.realpath(python)
        if python == current_python:
            # found best python and we're already running as it
            break
        # Try whether this Python works
        try:
            subprocess.check_call([python, "-c", "print(1)"],
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        except subprocess.CalledProcessError:
            continue
        argv = [os.path.basename(python)] + sys.argv
        os.environ["APPENV_NEWEST_PYTHON"] = python
        os.execv(python, argv)


def main():
    ensure_newest_python()

    # clear PYTHONPATH variable to get a defined environment
    # XXX this is a bit of history. not sure whether its still needed. keeping it
    # for good measure
    if "PYTHONPATH" in os.environ:
        del os.environ["PYTHONPATH"]

    # Prepare args for us and args for the actual target program.
    meta_argv = []
    argv = []

    # Preprocess sys.arv
    sys_argv = list(sys.argv)
    while sys_argv:
        arg = sys_argv.pop(0)
        if "appenv-" in arg:
            meta_arg = arg.replace("appenv-", "")
            meta_argv.append(meta_arg)
            if meta_arg == "run":
                # Hack, hack.
                meta_argv.append(sys_argv.pop(0))
        else:
            argv.append(arg)

    default_appname = os.path.splitext(os.path.basename(__file__))[0]

    # Parse the appenv arguments
    meta_parser = argparse.ArgumentParser()
    meta_parser.add_argument("-u",
                             "--unclean",
                             action="store_true",
                             help="Use an unclean working environment.")

    meta_parser.add_argument("--appname", default=default_appname)
    meta_parser.add_argument("--appenvdir", default="." + default_appname)
    meta_parser.set_defaults(func=run_app)
    meta_parser.add_argument("--base",
                             default=os.path.abspath(
                                 os.path.dirname(__file__)))

    subparsers = meta_parser.add_subparsers()
    p = subparsers.add_parser("update-lockfile", help="Update the lock file.")
    p.set_defaults(func=update_lockfile)

    p = subparsers.add_parser("init", help="Create a new appenv project.")
    p.set_defaults(func=init)

    p = subparsers.add_parser("reset", help="Reset the environment.")
    p.set_defaults(func=reset)

    p = subparsers.add_parser(
        "python", help="Spawn the embedded Python interpreter REPL")
    p.set_defaults(func=python)

    p = subparsers.add_parser(
        "run", help="Run a command from the venv's bin/ directory")
    p.add_argument("command")
    p.set_defaults(func=run)

    meta_args = meta_parser.parse_args(meta_argv)

    if not os.path.exists(meta_args.appenvdir):
        os.makedirs(meta_args.appenvdir)

    meta_args.func(argv, meta_args)


if __name__ == "__main__":
    main()
