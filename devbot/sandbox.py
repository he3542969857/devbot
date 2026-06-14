"""沙箱原语 —— rlimit 降权子进程跑 Python / 冒烟 / 带覆盖率跑 pytest / 从 diff 抽新增代码。

只放**原语**(不 import testgen,避免循环依赖):testgen / codegen / review 都依赖它,单向。
安全边界:rlimit 子进程级(CPU/内存/文件/进程数 + 超时杀进程组 + 隔离临时目录 + 剥离环境),
不做网络命名空间隔离(真隔离要 container/nsjail);`DEVBOT_CODEGEN_EXEC=0` 可一键关。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

try:
    import resource as _resource
except Exception:  # pragma: no cover
    _resource = None

_STD = {"pytest", "unittest", "typing", "math", "sys", "os", "re", "json", "collections",
        "itertools", "functools", "__future__", "random", "time", "decimal", "datetime",
        "abc", "dataclasses"}


def enabled() -> bool:
    return os.environ.get("DEVBOT_CODEGEN_EXEC", "1") == "1"


def _rlimits():  # pragma: no cover - 子进程 fork 后执行
    if _resource is None:
        return
    _resource.setrlimit(_resource.RLIMIT_CPU, (8, 10))
    try:
        _resource.setrlimit(_resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    except Exception:
        pass
    _resource.setrlimit(_resource.RLIMIT_FSIZE, (16 * 1024 * 1024,) * 2)
    try:
        _resource.setrlimit(_resource.RLIMIT_NPROC, (96, 96))
    except Exception:
        pass
    try:
        _resource.setrlimit(_resource.RLIMIT_CORE, (0, 0))
    except Exception:
        pass


def run_python(workdir: str, args: list[str], timeout: int = 8) -> tuple[int, str, str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": workdir, "PYTHONDONTWRITEBYTECODE": "1",
           "NO_PROXY": "*", "PYTHONHASHSEED": "0"}
    try:
        p = subprocess.run([sys.executable] + args, cwd=workdir, env=env, preexec_fn=_rlimits,
                           start_new_session=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "")[-3000:], (p.stderr or "")[-3000:]
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT after %ss" % timeout
    except Exception as e:  # noqa: BLE001
        return -1, "", "sandbox error: %s" % e


def module_of_test(test_code: str) -> str:
    """猜测试想从哪个模块导入(testgen 常按函数名 from fib import fib)。"""
    for mm in re.finditer(r"^\s*(?:from|import)\s+(\w+)", test_code, re.M):
        if mm.group(1) not in _STD:
            return mm.group(1)
    return "solution"


def smoke_run(code: str, timeout: int = 6) -> tuple[bool, str]:
    """写 solution.py,import/执行看会不会在加载/运行期崩。返回 (ok, err)。"""
    d = tempfile.mkdtemp(prefix="sb_")
    try:
        with open(os.path.join(d, "solution.py"), "w", encoding="utf-8") as f:
            f.write(code)
        if "__main__" in code:
            rc, out, err = run_python(d, ["solution.py"], timeout)
        else:
            rc, out, err = run_python(d, ["-c", "import solution"], timeout)
        return (rc == 0), ("" if rc == 0 else (err or out).strip()[-600:])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def run_tests_with_coverage(code: str, test_code: str, timeout: int = 25) -> dict[str, Any]:
    """把 code 写成测试想导入的模块名、跑 pytest + coverage.py,返回通过数/覆盖率/未覆盖行。"""
    res = {"ran": False, "passed": 0, "total": 0, "coverage": None, "missing_lines": "", "note": ""}
    if not code or not test_code:
        res["note"] = "无代码或无测试"
        return res
    d = tempfile.mkdtemp(prefix="cov_")
    try:
        mod = module_of_test(test_code)
        with open(os.path.join(d, mod + ".py"), "w", encoding="utf-8") as f:
            f.write(code)
        with open(os.path.join(d, "test_gen.py"), "w", encoding="utf-8") as f:
            f.write(test_code)
        rc, out, err = run_python(
            d, ["-m", "coverage", "run", "--source", mod, "-m", "pytest", "-q", "test_gen.py"], timeout)
        blob = out + err
        mp = re.search(r"(\d+) passed", blob)
        mf = re.search(r"(\d+) failed", blob)
        me = re.search(r"(\d+) error", blob)
        res["passed"] = int(mp.group(1)) if mp else 0
        res["total"] = res["passed"] + (int(mf.group(1)) if mf else 0)
        res["ran"] = res["total"] > 0
        if not res["ran"]:
            res["note"] = "测试收集失败(%s)" % (re.search(r"\d+ errors?", blob).group(0) if me else "无用例")
            res["raw"] = blob[-400:]
            return res
        rc2, out2, _ = run_python(d, ["-m", "coverage", "report", "-m"], 12)
        for line in out2.splitlines():
            if line.strip().startswith(mod + ".py") or (mod in line and "%" in line):
                cm = re.search(r"(\d+)%", line)
                if cm:
                    res["coverage"] = int(cm.group(1))
                mm2 = re.search(r"%\s+([\d,\-\s]+)$", line)
                if mm2:
                    res["missing_lines"] = mm2.group(1).strip()
        res["note"] = "pytest %d/%d 通过, 覆盖率 %s%%" % (
            res["passed"], res["total"], res["coverage"] if res["coverage"] is not None else "?")
        return res
    except Exception as e:  # noqa: BLE001
        res["note"] = "覆盖率执行异常: %s" % str(e)[:120]
        return res
    finally:
        shutil.rmtree(d, ignore_errors=True)


def extract_added_python(diff: str) -> str:
    """从 unified diff 抽新增行(+ 开头、去掉 +++/@@ 头),拼成可解析代码。"""
    if not diff:
        return ""
    out = []
    for ln in diff.splitlines():
        if ln.startswith(("+++", "---", "@@", "diff ", "index ")):
            continue
        if ln.startswith("+"):
            out.append(ln[1:])
    return "\n".join(out)


def run_diff_exec_check(diff: str, llm: Any = None, timeout: int = 8) -> dict[str, Any]:
    """评审用:抽 PR diff 新增 Python → 语法 + 冒烟实跑。**只做可靠的"崩不崩"信号**,
    不在评审里自动造测试当裁判(那不可靠)。"""
    import ast as _ast
    code = extract_added_python(diff)
    if not code.strip() or not enabled():
        return {"ran": False, "note": "无可运行新增代码或执行已关闭"}
    try:
        tree = _ast.parse(code)
    except SyntaxError as e:
        return {"ran": True, "smoke_ok": False, "kind": "crash",
                "error": "新增代码语法错: %s (line %s)" % (e.msg, e.lineno)}
    if not any(isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)) for n in tree.body):
        return {"ran": False, "note": "新增代码无顶层函数/类、不自包含,跳过实跑"}
    ok, err = smoke_run(code, timeout=6)
    if not ok:
        kind = "deps" if re.search(r"ModuleNotFoundError|ImportError|No module named", err) else "crash"
        return {"ran": True, "smoke_ok": False, "kind": kind, "error": err}
    return {"ran": True, "smoke_ok": True, "error": ""}
