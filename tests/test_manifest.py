"""Checks the Python client against spec/methods.json, the single source shared with the TypeScript package.

The spec lives in the tiktok-live-agency (npm) repo; the spec checks run when that repo is checked out next
to this one, or when TIKTOOLS_SPEC_PATH points at a methods.json, and skip otherwise.
"""

import inspect
import json
import os
import unittest

import tiktok_live_agency as pkg
from tiktok_live_agency import AgencyClient, AsyncAgencyClient

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = os.environ.get("TIKTOOLS_SPEC_PATH") or os.path.join(HERE, "..", "..", "tiktok-live-agency", "spec", "methods.json")


@unittest.skipUnless(os.path.exists(SPEC), "needs spec/methods.json: check out tiktok-live-agency next to this repo or set TIKTOOLS_SPEC_PATH")
class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(SPEC, encoding="utf-8") as f:
            cls.spec = json.load(f)

    def test_every_method_exists_in_sync_and_async_clients(self) -> None:
        for m in self.spec["methods"]:
            name = m["py"]
            if m["kind"] == "local":
                self.assertTrue(callable(getattr(pkg, name, None)), f"export {name} missing")
                continue
            self.assertTrue(callable(getattr(AgencyClient, name, None)), f"AgencyClient.{name} missing")
            self.assertTrue(callable(getattr(AsyncAgencyClient, name, None)), f"AsyncAgencyClient.{name} missing")


class PackageShapeTests(unittest.TestCase):
    def test_async_generators_are_async(self) -> None:
        for name in ("iter_creators_to_recruit", "iter_new_creators", "watch_live"):
            self.assertTrue(inspect.isasyncgenfunction(getattr(AsyncAgencyClient, name)), name)
        self.assertTrue(inspect.iscoroutinefunction(AsyncAgencyClient.get_account))

    def test_no_required_dependencies(self) -> None:
        pyproject = os.path.join(HERE, "..", "pyproject.toml")
        with open(pyproject, encoding="utf-8") as f:
            self.assertIn("dependencies = []", f.read())


if __name__ == "__main__":
    unittest.main()
