#!/usr/bin/env python

from __future__ import print_function

import argparse
import errno
import hashlib
import imp
import inspect
import json
import os
import pexpect
import pip
import requests
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import unittest
import xml.etree.cElementTree as ET

from backports.shutil_which import which
from contextlib import contextmanager
from functools import wraps
from pexpect.exceptions import EOF, TIMEOUT
from termcolor import cprint

try:
    from shlex import quote
except ImportError:
    from pipes import quote

import config

__all__ = ["check", "Checks", "Child", "EOF", "Error", "File", "Mismatch", "valgrind"]


def main():

    # parse command line arguments
    print("about to parse arguments...", file=sys.stderr)
    parser = argparse.ArgumentParser()
    parser.add_argument("identifier", nargs=1)
    parser.add_argument("files", nargs="*")
    parser.add_argument("-d", "--debug",
                        action="store_true",
                        help="display machine-readable output")
    parser.add_argument("-l", "--local",
                        action="store_true",
                        help="run checks locally instead of uploading to cs50")
    parser.add_argument("--offline",
                        action="store_true",
                        help="run checks completely offline (implies --local)")
    parser.add_argument("--checkdir",
                        action="store",
                        nargs="?",
                        default="~/.local/share/check50",
                        help="specify directory containing the checks "
                             "(~/.local/share/check50 by default)")
    parser.add_argument("--log",
                        action="store_true",
                        help="display more detailed information about check results")
    parser.add_argument("-v", "--verbose",
                        action="store_true",
                        help="display the full tracebacks of any errors")

    config.args = parser.parse_args()
    config.args.checkdir = os.path.expanduser(config.args.checkdir)
    identifier = config.args.identifier[0]
    files = config.args.files

    if config.args.offline:
        config.args.local = True


    if not config.args.local:
        try:

            # Submit to check50 repo.
            import submit50
        except ImportError:
            raise InternalError("submit50 is not installed. Install submit50 and run check50 again.")
        else:
            submit50.run.verbose = config.args.verbose
            username, commit_hash = submit50.submit("check50", identifier)

            # Wait until payload comes back with check data.
            print("Running checks...", end="")
            sys.stdout.flush()
            pings = 0
            while True:

                # Terminate if no response.
                if pings > 45:
                    cprint("check50 is taking longer than normal!", "red", file=sys.stderr)
                    cprint("more info at: https://cs50.me/check50/results/{}/{}".format(username, commit_hash), "red", file=sys.stderr)
                    sys.exit(1)
                pings += 1

                # Query for check results.
                res = requests.post("https://cs50.me/check50/status/{}/{}".format(username, commit_hash))
                if res.status_code != 200:
                    continue
                payload = res.json()
                if payload["complete"] and payload["checks"] != []:
                    break
                print(".", end="")
                sys.stdout.flush()
                time.sleep(2)
            print()

            # Print results from payload.
            print_results(payload["checks"], config.args.log)
            print("detailed results: https://cs50.me/check50/results/{}/{}".format(username, commit_hash))
            sys.exit(0)

    # copy all files to temporary directory
    print("about to copy files to temporary dir...", file=sys.stderr)
    config.tempdir = tempfile.mkdtemp()
    src_dir = os.path.join(config.tempdir, "_")
    os.mkdir(src_dir)
    if len(files) == 0:
        files = os.listdir(".")
    for filename in files:
        copy(filename, src_dir)
    print("done copying files to temporary dir...", file=sys.stderr)

    print("about to import checks...", file=sys.stderr)
    checks = import_checks(identifier)
    print("done with import checks...", file=sys.stderr)

    # create and run the test suite
    print("about to create suite...", file=sys.stderr)
    suite = unittest.TestSuite()
    for case in config.test_cases:
        suite.addTest(checks(case))
    result = TestResult()
    print("about to run suite...", file=sys.stderr)
    suite.run(result)
    print("about to cleanup...", file=sys.stderr)
    cleanup()
    print("done with cleanup...", file=sys.stderr)

    # Get list of results from TestResult class
    results = result.results

    # print the results
    print("about to print results...", file=sys.stderr)
    if config.args.debug:
        print_json(results)
    else:
        print_results(results, log=config.args.log)
    print("done...", file=sys.stderr)


@contextmanager
def cd(path):
    """can be used with a `with` statement to temporarily change directories"""
    print("start cd...", file=sys.stderr)
    cwd = os.getcwd()
    try:
        os.chdir(path)
        yield
    finally:
        os.chdir(cwd)
    print("end cd...", file=sys.stderr)


def cleanup():
    """Remove temporary files at end of test."""
    print("start cleanup...", file=sys.stderr)
    if config.tempdir:
        shutil.rmtree(config.tempdir)
    print("end cleanup...", file=sys.stderr)


def copy(src, dst):
    """Copy src to dst, copying recursively if src is a directory"""
    print("start copy...", file=sys.stderr)
    try:
        shutil.copytree(src, os.path.join(dst, os.path.basename(src)))
    except (OSError, IOError) as e:
        if e.errno == errno.ENOTDIR:
            shutil.copy(src, dst)
        else:
            raise
    print("end copy...", file=sys.stderr)


def excepthook(cls, exc, tb):
    print("excepthook...", file=sys.stderr)
    cleanup()

    # Class is a BaseException, better just quit
    if not issubclass(cls, Exception):
        print()
        return

    if cls is InternalError:
        cprint(exc.msg, "red", file=sys.stderr)
    elif any(issubclass(cls, err) for err in [IOError, OSError]) and exc.errno == errno.ENOENT:
        cprint("{} not found".format(exc.filename), "red", file=sys.stderr)
    else:
        cprint("Sorry, something's wrong! Let sysadmins@cs50.harvard.edu know!", "red", file=sys.stderr)

    if config.args.verbose:
        traceback.print_exception(cls, exc, tb)


sys.excepthook = excepthook


def print_results(results, log=False):
    print("about to start printing results...", file=sys.stderr)
    for result in results:
        if result["status"] == Checks.PASS:
            cprint(":) {}".format(result["description"]), "green")
        elif result["status"] == Checks.FAIL:
            cprint(":( {}".format(result["description"]), "red")
            if result["rationale"] is not None:
                cprint("    {}".format(result["rationale"]), "red")
        elif result["status"] == Checks.SKIP:
            cprint(":| {}".format(result["description"]), "yellow")
            cprint("    {}".format(result.get("rationale") or "check skipped"), "yellow")

        if log:
            for line in result["test"].log:
                print("    {}".format(line))
    print("done with printing results...", file=sys.stderr)


def print_json(results):
    output = []
    print("about to iterate through results...", file=sys.stderr)
    for result in results:
        obj = {
            "name": result["test"]._testMethodName,
            "status": result["status"],
            "description": result["description"],
            "helpers": result["helpers"],
            "log": result["test"].log,
            "rationale": str(result["rationale"]) if result["rationale"] else None
        }

        try:
            obj["mismatch"] = {
                "expected": result["rationale"].expected,
                "actual": result["rationale"].actual
            }
        except AttributeError:
            pass

        output.append(obj)
    print("about to print results...", file=sys.stderr)
    print(json.dumps(output))


def import_checks(identifier):
    """
    Given an identifier of the form path/to/check@org/repo, clone
    the checks from github.com/org/repo (defaulting to cs50/checks
    if there is no @) into config.args.checkdir. Then extract child
    of Check class from path/to/check/check50/__init__.py and return it

    Throws ImportError on error
    """
    print("start import checks...", file=sys.stderr)
    try:
        print("trying to split at @...", file=sys.stderr)
        slug, repo = identifier.split("@")
    except ValueError:
        print("got exception, assuming cs50/checks...", file=sys.stderr)
        slug, repo = identifier, "cs50/checks"

    try:
        print("trying to split repo at /...", file=sys.stderr)
        org, repo = repo.split("/")
    except ValueError:
        print("repo not well formatted...", file=sys.stderr)
        raise InternalError("expected repository to be of the form username/repository, but got \"{}\"".format(repo))

    print("determining directories...", file=sys.stderr)
    checks_root = os.path.join(config.args.checkdir, org, repo)
    config.check_dir = os.path.join(checks_root, slug.replace("/", os.sep), "check50")

    print("setting up command for clone or pulling", file=sys.stderr)
    if not config.args.offline:
        if os.path.exists(checks_root):
            print("going to pull", file=sys.stderr)
            command = ["git", "-C", checks_root, "pull", "origin", "master"]

        else:
            print("going to clone...", file=sys.stderr)
            command = ["git", "clone", "https://github.com/{}/{}".format(org, repo), checks_root]

        # Can't use subprocess.DEVNULL because it requires python 3.3
        print("redirecting stdout and stderr...", file=sys.stderr)
        stdout = stderr = None if config.args.verbose else open(os.devnull, "wb")

        # Update checks via git
        try:
            print("about to make call to clone or check...", file=sys.stderr)
            subprocess.check_call(command, stdout=stdout, stderr=stderr)
            print("done with call to clone or check...", file=sys.stderr)
        except subprocess.CalledProcessError:
            raise InternalError("failed to clone checks")


    # Install any dependencies from requirements.txt either in the root of the repository or in the directory of the specific check
    print("about to search for dependencies...", file=sys.stderr)
    for dir in [checks_root, os.path.dirname(config.check_dir)]:
        requirements = os.path.join(dir, "requirements.txt")
        if os.path.exists(requirements):
            args = ["install", "-r", requirements]
            # If we are not in a virtualenv, we need --user
            if not hasattr(sys, "real_prefix"):
                args.append("--user")

            if not config.args.verbose:
                args += ["--quiet"] * 3

            try:
                code = pip.main(args)
            except SystemExit as e:
                code = e.code

            if code:
                raise InternalError("failed to install dependencies in ({})".format(requirements[len(config.args.checkdir)+1:]))
    print("done with search for dependencies...", file=sys.stderr)

    try:
        # Import module from file path directly
        print("about to try to load the module...", file=sys.stderr)
        module = imp.load_source(slug, os.path.join(config.check_dir, "__init__.py"))
        print("loaded the module...", file=sys.stderr)
        # Ensure that there is exactly one class decending from Checks defined in this package
        print("ensuring that checks exist...", file=sys.stderr)
        checks, = (cls for _, cls in inspect.getmembers(module, inspect.isclass)
                       if hasattr(cls, "_Checks__sentinel")
                           and cls.__module__.startswith(slug))
    except (OSError, IOError) as e:
        if e.errno != errno.ENOENT:
            raise
    except ValueError:
        pass
    else:
        return checks

    raise InternalError("invalid identifier")


def import_from(path):
    """helper function to make it easier for a check to import another check"""
    with cd(config.check_dir):
        abspath = os.path.abspath(os.path.join(path, "check50", "__init__.py"))
    return imp.load_source(os.path.basename(path), abspath)


class TestResult(unittest.TestResult):
    results = []

    def __init__(self):
        super(TestResult, self).__init__(self)

    def addSuccess(self, test):
        """Handle completion of test, regardless of outcome."""
        print("got a successful check: {} with result {}...".format(test.shortDescription(), test.result), file=sys.stderr)
        self.results.append({
            "description": test.shortDescription(),
            "helpers": test.helpers,
            "log": test.log,
            "rationale": test.rationale,
            "status": test.result,
            "test": test
        })

    def addError(self, test, err):
        print("got an error...", file=sys.stderr)
        self.results.append({
            "description": test.shortDescription(),
            "helpers": test.helpers,
            "log": test.log,
            "rationale": err[1],
            "status": Checks.FAIL,
            "test": test
        })
        cprint("check50 ran into an error while running checks.", "red", file=sys.stderr)
        print(err[1], file=sys.stderr)
        traceback.print_tb(err[2])
        sys.exit(1)


def valgrind(func):
    if config.test_cases[-1] == func.__name__:
        frame = traceback.extract_stack(limit=2)[0]
        raise InternalError("invalid check in {} on line {} of {}:\n"
                            "@valgrind must be placed below @check"\
                            .format(frame.name, frame.lineno, frame.filename))
    @wraps(func)
    def wrapper(self):
        if not which("valgrind"):
            raise Error("valgrind not installed", result=Checks.SKIP)

        self._valgrind = True
        try:
            func(self)
            self._check_valgrind()
        finally:
            self._valgrind = False
    return wrapper


# decorator for checks
def check(dependency=None):
    def decorator(func):

        # add test to list of test, in order of declaration
        config.test_cases.append(func.__name__)
        @wraps(func)
        def wrapper(self):

            # check if dependency failed
            print("running check {}".format(func.__name__), file=sys.stderr)
            print("checking if dependency is satisfied...", file=sys.stderr)
            if dependency and config.test_results.get(dependency) != Checks.PASS:
                self.result = config.test_results[func.__name__] = Checks.SKIP
                self.rationale = "can't check until a frown turns upside down"
                return

            # move files into this check's directory
            print("moving files into this check dir...", file=sys.stderr)
            self.dir = dst_dir = os.path.join(config.tempdir, self._testMethodName)
            src_dir = os.path.join(config.tempdir, dependency or "_")
            shutil.copytree(src_dir, dst_dir)

            os.chdir(self.dir)
            # run the test, catch failures
            print("trying to run test and check failures...", file=sys.stderr)
            try:
                func(self)
            except Error as e:
                print("caught an exception...", file=sys.stderr)
                self.rationale = e.rationale
                self.helpers = e.helpers
                result = e.result
            else:
                result = Checks.PASS

            self.result = config.test_results[func.__name__] = result

        return wrapper
    return decorator


class File(object):
    """Generic class to represent file in check directory."""
    def __init__(self, filename):
        self.filename = filename

    def read(self):
        with File._open(self.filename) as f:
            return f.read()

    @staticmethod
    def _open(file, mode="r"):
        if sys.version_info < (3, 0):
            return open(file, mode + "U")
        else:
            return open(file, mode, newline="\n")


# wrapper class for pexpect child
class Child(object):
    def __init__(self, test, child):
        self.test = test
        self.child = child
        self.output = []
        self.exitstatus = None

    def stdin(self, line, prompt=True, timeout=3):
        print("stdin...", file=sys.stderr)
        if line == EOF:
            self.test.log.append("sending EOF...")
        else:
            self.test.log.append("sending input {}...".format(line))

        if prompt:
            try:
                print("expecting a prompt on stdin...", file=sys.stderr)
                self.child.expect(".+", timeout=timeout)
            except TIMEOUT:
                raise Error("expected prompt for input, found none")

        if line == EOF:
            self.child.sendeof()
        else:
            self.child.sendline(line)
        return self

    def stdout(self, output=None, str_output=None, timeout=3):
        print("stdout...", file=sys.stderr)
        if output is None:
            print("in stdout, going to wait and check output...", file=sys.stderr)
            return self.wait(timeout).output

        # Files should be interpreted literally, anything else shouldn't be
        try:
            print("trying to read output...", file=sys.stderr)
            output = output.read()
        except AttributeError:
            expect = self.child.expect
        else:
            expect = self.child.expect_exact

        if output == EOF:
            str_output = "EOF"
        else:
            if str_output is None:
                str_output = output
            output = output.replace("\n", "\r\n")


        self.test.log.append("checking for output \"{}\"...".format(str_output))

        try:
            print("trying to expect desired output...", file=sys.stderr)
            expect(output, timeout=timeout)
            print("done with expecting output...", file=sys.stderr)
        except EOF:
            result = self.child.before + self.child.buffer
            if self.child.after != EOF:
                result += self.child.after
            raise Error(Mismatch(str_output, result.replace("\r\n", "\n")))
        except TIMEOUT:
            raise Error("timed out while waiting for {}".format(Mismatch.raw(str_output)))

        # If we expected EOF and we still got output, report an error
        if output == EOF and self.child.before:
            raise Error(Mismatch(EOF, self.child.before.replace("\r\n", "\n")))

        return self

    def reject(self, timeout=3):
        print("checking that input was rejected...", file=sys.stderr)
        self.test.log.append("checking that input was rejected...")
        try:
            print("about to pexpect in reject...", file=sys.stderr)
            self.child.expect(".+", timeout=timeout)
            print("done with pexpect in reject...", file=sys.stderr)
            self.child.sendline("")
        except OSError:
            self.test.fail()
        except TIMEOUT:
            raise Error("timed out while waiting for input to be rejected")
        return self

    def exit(self, code=None, timeout=3):
        print("exit: about to wait...", file=sys.stderr)
        self.wait(timeout)
        print("exit: done with wait...", file=sys.stderr)

        if code is None:
            return self.exitstatus

        self.test.log.append("checking that program exited with status {}...".format(code))
        if self.exitstatus != code:
            raise Error("expected exit code {}, not {}".format(code, self.exitstatus))
        return self

    def wait(self, timeout=3):
        print("waiting: about to start waiting...", file=sys.stderr)
        end = time.time() + timeout
        while time.time() <= end:
            if not self.child.isalive():
                break
            try:
                bytes = self.child.read_nonblocking(size=1024, timeout=0)
            except TIMEOUT:
                pass
            except EOF:
                break
            else:
                self.output.append(bytes)
        else:
            raise Error("timed out while waiting for program to exit")
        print("done waiting", file=sys.stderr)

        # Read any remaining data in pipe
        print("about to read in other bytes", file=sys.stderr)
        while True:
            try:
                bytes = self.child.read_nonblocking(size=1024, timeout=0)
            except (TIMEOUT, EOF):
                break
            else:
                self.output.append(bytes)
        print("done with reading in other bytes", file=sys.stderr)

        self.output = "".join(self.output).replace("\r\n", "\n").lstrip("\n")
        self.kill()
        self.exitstatus = self.child.exitstatus
        return self

    def kill(self):
        self.child.close(force=True)
        return self

class Checks(unittest.TestCase):
    PASS = True
    FAIL = False
    SKIP = None

    _valgrind_log = "valgrind.xml"
    _valgrind = False

    # Here so we can properly check subclasses even when child is imported from another module
    __sentinel = None

    def tearDown(self):
        while self.children:
            self.children.pop().kill()

    def __init__(self, method_name):
        super(Checks, self).__init__(method_name)
        self.result = self.FAIL
        self.rationale = None
        self.helpers = None
        self.log = []
        self.children = []

    def diff(self, f1, f2):
        """Returns boolean indicating whether or not the files are different"""
        if type(f1) == File:
            f1 = f1.filename
        if type(f2) == File:
            f2 = f2.filename
        return bool(self.spawn("diff {} {}".format(quote(f1), quote(f2)))
                        .wait()
                        .exitstatus)

    def require(self, *paths):
        """Asserts that all paths exist."""
        print("starting require", file=sys.stderr)
        for path in paths:
            self.log.append("Checking that {} exists...".format(path))
            if not os.path.exists(path):
                raise Error("{} not found".format(path))
        print("ending require", file=sys.stderr)

    def hash(self, filename):
        """Hashes a file using SHA-256."""

        # Assert that file exists.
        if type(filename) == File:
            filename = filename.filename
        self.require(filename)

        # https://stackoverflow.com/a/22058673
        sha256 = hashlib.sha256()
        with open(filename, "rb") as f:
            while True:
                data = f.read(65536)
                if not data:
                    break
                sha256.update(data)
        return sha256.hexdigest()

    def spawn(self, cmd, env=None):
        """Spawns a new child process."""
        print("about to spawn", file=sys.stderr)
        if self._valgrind:
            self.log.append("running valgrind {}...".format(cmd))
            cmd = "valgrind --show-leak-kinds=all --xml=yes --xml-file={} -- {}" \
                        .format(os.path.join(self.dir, self._valgrind_log), cmd)
        else:
            self.log.append("running {}...".format(cmd))

        if env is None:
            env = {}
        env = os.environ.update(env)

        # Workaround for OSX pexpect bug http://pexpect.readthedocs.io/en/stable/commonissues.html#truncated-output-just-before-child-exits
        # Workaround from https://github.com/pexpect/pexpect/issues/373
        cmd = "bash -c {}".format(quote(cmd))
        if sys.version_info < (3, 0):
            child = pexpect.spawn(cmd, echo=False, env=env)
        else:
            print("going to run pexpect.spawn", file=sys.stderr)
            child = pexpect.spawnu(cmd, encoding="utf-8", echo=False, env=env)
            print("done with run pexpect.spawn", file=sys.stderr)

        self.children.append(Child(self, child))
        return self.children[-1]

    def add(self, *paths):
        """Copies a file to the temporary directory."""
        cwd = os.getcwd()
        with cd(config.check_dir):
            for path in paths:
                copy(path, cwd)

    def append_code(self, filename, codefile):
        with open(codefile.filename, "r") as code, \
                open(os.path.join(self.dir, filename), "a") as f:
            f.write("\n")
            f.write(code.read())

    def replace_fn(self, old_fn, new_fn, filename):
        self.spawn("sed -i='' -e 's/callq\t_{}/callq\t_{}/g' {}".format(old_fn, new_fn, filename))
        self.spawn("sed -i='' -e 's/callq\t{}/callq\t{}/g' {}".format(old_fn, new_fn, filename))

    def _check_valgrind(self):
        """Log and report any errors encountered by valgrind"""
        # Load XML file created by valgrind
        xml = ET.ElementTree(file=os.path.join(self.dir, self._valgrind_log))

        self.log.append("checking for valgrind errors... ")

        # Ensure that we don't get duplicate error messages
        reported = set()
        for error in xml.iterfind("error"):
            # Type of error valgrind encountered
            kind = error.find("kind").text

            # Valgrind's error message
            what = error.find("xwhat/text" if kind.startswith("Leak_") else "what").text

            # Error message that we will report
            msg = ["\t", what]

            # Find first stack frame within student's code
            for frame in error.iterfind("stack/frame"):
                obj = frame.find("obj")
                if obj is not None and os.path.dirname(obj.text) == self.dir:
                    location = frame.find("file"), frame.find("line")
                    if None not in location:
                        msg.append(": (file: {}, line: {})".format(location[0].text, location[1].text))
                    break

            msg = "".join(msg)
            if msg not in reported:
                self.log.append(msg)
                reported.add(msg)

        # Only raise exception if we encountered errors
        if reported:
            raise Error("valgrind tests failed; rerun with --log for more information.")


class Mismatch(object):
    """Class which represents that expected output did not match actual output."""
    def __init__(self, expected, actual):
        self.expected = expected
        self.actual = actual

    def __str__(self):
        return "expected {}, not {}".format(self.raw(self.expected),
                                             self.raw(self.actual))

    def __repr__(self):
        return "Mismatch(expected={}, actual={})".format(repr(expected), repr(actual))

    @staticmethod
    def raw(s):
        """Get raw representation of s, truncating if too long"""

        if type(s) == list:
            s = "\n".join(s)

        if s == EOF:
            return "EOF"

        s = repr(s)  # get raw representation of string
        s = s[1:-1]  # strip away quotation marks
        if len(s) > 15:
            s = s[:15] + "..."  # truncate if too long
        return "\"{}\"".format(s)



class Error(Exception):
    """Class to wrap errors in students' checks."""
    def __init__(self, rationale=None, helpers=None, result=Checks.FAIL):
        self.rationale = rationale
        self.helpers = helpers
        self.result = result


class InternalError(Exception):
    """Error during execution of check50."""
    def __init__(self, msg):
        self.msg = msg


if __name__ == "__main__":
    main()
