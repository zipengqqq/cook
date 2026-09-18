# 让 pytest 把项目根目录加进 sys.path，测试里才能 `from app.xxx import ...`
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 导入 main.py 就会装配日志（test_api.py 必须导入它），不拦一下的话每跑一次测试
# 都会往真实的 logs/app.log 里灌一堆请求日志。等真去排查问题时，这些测试噪音
# 和线上记录混在一起，反而不好认。用 setdefault 是为了不覆盖你自己设的 LOG_DIR。
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="cook-test-logs-"))
