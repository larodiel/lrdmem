"""Test package: keep the suite away from the developer's real lrdmem config."""

import os
import tempfile

os.environ["LRDMEM_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="lrdmem-test-"), "absent", "config.json")
