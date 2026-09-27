import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class DBTestCase(unittest.TestCase):
    """每个测试类使用独立的数据目录和数据库。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dwtest_"))
        os.environ["DW_DATA_DIR"] = str(cls.tmp)
        os.environ["DW_MASTER_KEY"] = "test-master-key-0123456789"
        os.environ["DW_COOKIE_SECURE"] = "false"
        os.environ["DW_ACCEL_REDIRECT"] = "false"
        os.environ.setdefault("DW_SANDBOX", "rlimit")
        from app import config, db
        config.reset_settings()
        db.close_thread_conn()
        db.migrate()

    @classmethod
    def tearDownClass(cls):
        from app import db
        db.close_thread_conn()
        shutil.rmtree(cls.tmp, ignore_errors=True)
