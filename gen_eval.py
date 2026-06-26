# -*- coding: utf-8 -*-
"""codegen / testgen 离线生成质量 Harness：一批任务带验收测试,真 LLM 生成→沙箱验收,
量 codegen 成功率/平均修复轮数、testgen 平均覆盖率/通过率。补"只有单点实测、没有整体成功率"的洞。"""
import os, shutil, sys, tempfile, time
from devbot.codegen_agent import generate_code
from devbot.testgen_agent import generate_tests
from devbot import sandbox

def run_check(gen_code, check_code):
    d = tempfile.mkdtemp(prefix="ge_")
    try:
        with open(os.path.join(d, "solution.py"), "w") as f: f.write(gen_code or "")
        with open(os.path.join(d, "check.py"), "w") as f: f.write(check_code)
        rc, out, err = sandbox.run_python(d, ["check.py"], timeout=8)
        return rc == 0, (err or out or "")[-160:]
    finally:
        shutil.rmtree(d, ignore_errors=True)

CODEGEN = [
 ("clamp", "实现函数 clamp(x, lo, hi):把 x 限制在 [lo, hi] 区间内并返回",
  "from solution import clamp\nassert clamp(5,0,10)==5\nassert clamp(-1,0,10)==0\nassert clamp(20,0,10)==10\nprint('ok')"),
 ("is_palindrome", "实现函数 is_palindrome(s):判断字符串 s 是否回文,返回 True/False",
  "from solution import is_palindrome\nassert is_palindrome('aba')\nassert not is_palindrome('ab')\nassert is_palindrome('')\nprint('ok')"),
 ("fib", "实现函数 fib(n):返回第 n 个斐波那契数,fib(0)=0,fib(1)=1",
  "from solution import fib\nassert fib(0)==0\nassert fib(1)==1\nassert fib(10)==55\nprint('ok')"),
 ("flatten", "实现函数 flatten(lst):把任意嵌套的列表展平成一维列表返回",
  "from solution import flatten\nassert flatten([1,[2,[3,4]],5])==[1,2,3,4,5]\nassert flatten([])==[]\nprint('ok')"),
 ("roman_to_int", "实现函数 roman_to_int(s):罗马数字字符串转整数",
  "from solution import roman_to_int\nassert roman_to_int('III')==3\nassert roman_to_int('IV')==4\nassert roman_to_int('XIV')==14\nprint('ok')"),
 ("group_parity", "实现函数 group_parity(nums):返回 (偶数列表, 奇数列表) 二元组",
  "from solution import group_parity\nassert group_parity([1,2,3,4])==([2,4],[1,3])\nprint('ok')"),
]

TESTGEN = [
 ("classify", "def classify(n):\n    if n < 0:\n        return 'neg'\n    if n == 0:\n        return 'zero'\n    return 'pos'"),
 ("clamp", "def clamp(x, lo, hi):\n    if x < lo:\n        return lo\n    if x > hi:\n        return hi\n    return x"),
 ("safe_div", "def safe_div(a, b):\n    if b == 0:\n        raise ValueError('div by zero')\n    return a / b"),
]

print("=== codegen 生成质量 ===")
cp = 0; rr = 0; t0 = time.time()
for name, prompt, check in CODEGEN:
    out = generate_code(prompt, language="python")
    ok, msg = run_check(out.get("generated_code", ""), check)
    cp += ok; rr += out.get("repair_rounds", 0)
    print("  %-14s 验收=%s 修复轮=%d %s" % (name, "PASS" if ok else "FAIL", out.get("repair_rounds",0), "" if ok else "| "+msg))
print("  >>> 成功率 %d/%d=%.0f%%  平均修复轮 %.1f  耗时 %ds" % (cp,len(CODEGEN),100*cp/len(CODEGEN),rr/len(CODEGEN),time.time()-t0))

print("\n=== testgen 生成质量 ===")
tc = 0; cov = 0; t1 = time.time()
for name, code in TESTGEN:
    out = generate_tests(code=code, language="python", execute=True)
    c = out.get("coverage"); passed = out.get("tests_passed",0); total = out.get("tests_total",0)
    ran = out.get("executed"); cov += (c or 0); tc += (ran and total>0)
    print("  %-10s 跑起来=%s 测试 %d/%d 覆盖率=%s%% 修复轮=%d" % (name, ran, passed, total, c, out.get("repair_rounds",0)))
print("  >>> 能跑率 %d/%d  平均覆盖率 %.0f%%  耗时 %ds" % (tc,len(TESTGEN),cov/len(TESTGEN),time.time()-t1))
sys.exit(0)
