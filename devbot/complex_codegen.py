"""复杂场景代码生成 —— 编排:需求拆解 → 带依赖逐组件生成 → 装配 → 整体验证 → 定位修复。

解决现有 codegen 只能写**单函数自包含**场景的短板。复杂需求往往要**多个互相依赖的组件**
(类 + 工厂 + 辅助函数),单次生成一段塞不进、也验证不了。本模块把它拆成确定性编排骨架:

    需求
     │ ① plan: 拆成有依赖关系的组件清单 (ComponentSpec[])
     ▼
    ② 拓扑排序: 被依赖的先生成
     │
     ▼
    ③ 逐组件生成: 生成第 N 个时, 把已装配的前 N-1 个当上下文喂进去
     │            (这是"复杂"区别于"自包含单函数"的关键——后面的组件能调前面的)
     ▼
    ④ 增量装配 + 导入冒烟: 每加一个组件就验证整体还 import 得起来
     │
     ▼
    ⑤ 整体验证: 装配成一个模块, 跑集成检查 (真实执行, 不是 LLM 判断)
     │
     ▼
    ⑥ 定位修复 (≤N 轮): 从 traceback 找出错的组件, 只重生成那几个, 保留其余, 重验

LLM 是**可插拔接口** `Generator`:生产用 `LlmGenerator`(包 devbot 的 LlmClient);
测试/离线用确定性 `Generator` 注入,使整条编排可复现、可单测。

执行验证复用本目录真实的 `sandbox.py`:POSIX 委托 `sandbox.run_python`(带 rlimit 降权),
Windows 退化为带超时的可移植子进程(隔离临时目录 + 剥离环境仍在,仅缺 rlimit)。
全程 robust、绝不抛给调用方。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

try:  # 复用本目录真实沙箱(POSIX 上有 rlimit);拿不到也能跑(退化可移植子进程)
    from . import sandbox as _sandbox
except Exception:  # pragma: no cover
    _sandbox = None

MAX_REPAIR = 2


# ────────────────────────────── 数据模型 ──────────────────────────────
@dataclass
class ComponentSpec:
    """一个待生成组件:函数 / 类,带它依赖的其它组件名。"""
    name: str
    kind: str = "function"          # function | class
    description: str = ""
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    """需求拆解结果:组件清单 + 一段集成检查代码(纯 assert,验证组件协作)。"""
    components: list[ComponentSpec]
    check_code: str = ""            # import 各组件并 assert 其协作行为


@dataclass
class BuildResult:
    requirement: str
    plan: Optional[Plan] = None
    components_code: dict[str, str] = field(default_factory=dict)
    assembled_code: str = ""
    verified: bool = False
    verify_error: str = ""
    repair_rounds: int = 0
    repaired_components: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "components": [c.name for c in (self.plan.components if self.plan else [])],
            "assembled_code": self.assembled_code,
            "verified": self.verified,
            "verify_error": self.verify_error,
            "repair_rounds": self.repair_rounds,
            "repaired_components": self.repaired_components,
            "note": self.note,
        }


# ────────────────────────────── LLM 可插拔接口 ──────────────────────────────
class Generator(Protocol):
    """生成器接口:生产实现包 LLM,测试实现注入确定性代码,使编排可复现。"""

    def plan(self, requirement: str) -> Plan: ...

    def generate(self, requirement: str, spec: ComponentSpec,
                 prior_code: str, repair_hint: str = "") -> str: ...


# ────────────────────────────── 拓扑排序 ──────────────────────────────
def _toposort(components: list[ComponentSpec]) -> list[ComponentSpec]:
    """按 depends_on 排序:被依赖的先生成。有环则退回原序(不崩)。"""
    by_name = {c.name: c for c in components}
    indeg = {c.name: 0 for c in components}
    for c in components:
        for d in c.depends_on:
            if d in by_name:
                indeg[c.name] += 1
    ready = [n for n, d in indeg.items() if d == 0]
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for c in components:
            if n in c.depends_on and c.name in indeg:
                indeg[c.name] -= 1
                if indeg[c.name] == 0:
                    ready.append(c.name)
    if len(order) != len(components):          # 有环:剩下的按原序补上
        order += [c.name for c in components if c.name not in order]
    return [by_name[n] for n in order]


# ────────────────────────────── 装配 ──────────────────────────────
def _assemble(parts: dict[str, str], order: list[ComponentSpec]) -> str:
    """把各组件源码拼成一个模块:import 行去重上提 + 组件按依赖序排列。"""
    imports: list[str] = []
    seen_imp: set[str] = set()
    bodies: list[str] = []
    for spec in order:
        code = parts.get(spec.name, "").strip()
        if not code:
            continue
        body_lines = []
        for ln in code.splitlines():
            if re.match(r"^\s*(import |from )", ln) and ln.strip() not in seen_imp:
                seen_imp.add(ln.strip())
                imports.append(ln.strip())
            elif re.match(r"^\s*(import |from )", ln):
                continue                       # 重复 import 丢弃
            else:
                body_lines.append(ln)
        bodies.append("\n".join(body_lines).strip())
    head = "\n".join(imports)
    return (head + "\n\n\n" if head else "") + "\n\n\n".join(b for b in bodies if b) + "\n"


# ────────────────────────────── 执行验证(平台感知,复用真实沙箱) ──────────────────────────────
def _run(workdir: str, args: list[str], timeout: int = 10) -> tuple[int, str, str]:
    if os.name == "posix" and _sandbox is not None and getattr(_sandbox, "enabled", lambda: True)():
        return _sandbox.run_python(workdir, args, timeout)       # 带 rlimit 降权
    # Windows / 无沙箱:可移植子进程——隔离 cwd + 剥离环境 + 超时(缺 rlimit)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": workdir,
           "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "*", "PYTHONHASHSEED": "0",
           "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    try:
        p = subprocess.run([sys.executable] + args, cwd=workdir, env=env,
                           capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "")[-3000:], (p.stderr or "")[-3000:]
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT after %ss" % timeout
    except Exception as e:  # noqa: BLE001
        return -1, "", "exec error: %s" % e


def verify(assembled_code: str, check_code: str, timeout: int = 10) -> tuple[bool, str]:
    """装配模块 + 集成检查写临时目录, 真实执行 check.py。返回 (ok, err)。"""
    if not assembled_code.strip():
        return False, "空代码"
    d = tempfile.mkdtemp(prefix="cc_")
    try:
        with open(os.path.join(d, "solution.py"), "w", encoding="utf-8") as f:
            f.write(assembled_code)
        check = check_code or "import solution\n"
        with open(os.path.join(d, "check.py"), "w", encoding="utf-8") as f:
            f.write(check)
        rc, out, err = _run(d, ["check.py"], timeout)
        return (rc == 0), ("" if rc == 0 else (err or out).strip()[-800:])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _import_smoke(assembled_code: str) -> tuple[bool, str]:
    """增量装配后的导入冒烟:整体 import 得起来吗。"""
    return verify(assembled_code, "import solution\n", timeout=6)


def _failing_components(err: str, components: list[ComponentSpec]) -> list[str]:
    """从 traceback / 错误文本里找出涉及的组件名(定位修复用)。"""
    hits = [c.name for c in components if re.search(r"\b%s\b" % re.escape(c.name), err)]
    return hits


# ────────────────────────────── 编排主体 ──────────────────────────────
class ComplexCodegen:
    def __init__(self, generator: Generator, max_repair: int = MAX_REPAIR):
        self.gen = generator
        self.max_repair = max_repair

    def build(self, requirement: str) -> BuildResult:
        res = BuildResult(requirement=requirement)
        # ① 拆解
        try:
            plan = self.gen.plan(requirement)
        except Exception as e:  # noqa: BLE001
            res.note = "拆解失败: %s" % e
            return res
        if not plan.components:
            res.note = "拆解出 0 组件"
            return res
        res.plan = plan

        # ② 拓扑排序 → ③④ 逐组件生成 + 增量装配 + 导入冒烟
        order = _toposort(plan.components)
        parts: dict[str, str] = {}
        for spec in order:
            prior = _assemble(parts, order)
            code = self._gen_one(requirement, spec, prior)
            parts[spec.name] = code
            assembled = _assemble(parts, order)
            ok, err = _import_smoke(assembled)
            tries = 0
            while not ok and tries < self.max_repair:   # 单组件导入级修复
                tries += 1
                code = self._gen_one(requirement, spec, prior, repair_hint="导入失败:\n" + err)
                parts[spec.name] = code
                assembled = _assemble(parts, order)
                ok, err = _import_smoke(assembled)
            if tries:
                res.repaired_components.append("%s(import×%d)" % (spec.name, tries))
        res.components_code = dict(parts)
        res.assembled_code = _assemble(parts, order)

        # ⑤ 整体集成验证
        ok, err = verify(res.assembled_code, plan.check_code)
        res.verified, res.verify_error = ok, err

        # ⑥ 定位修复:从 traceback 找出错组件, 只重生成那几个
        while not res.verified and res.repair_rounds < self.max_repair:
            res.repair_rounds += 1
            targets = _failing_components(err, plan.components) or [order[-1].name]
            for name in targets:
                spec = next(c for c in plan.components if c.name == name)
                prior = _assemble({k: v for k, v in parts.items() if k != name}, order)
                parts[name] = self._gen_one(requirement, spec, prior,
                                            repair_hint="集成检查失败:\n" + err)
                res.repaired_components.append("%s(integ r%d)" % (name, res.repair_rounds))
            res.assembled_code = _assemble(parts, order)
            ok, err = verify(res.assembled_code, plan.check_code)
            res.verified, res.verify_error = ok, err

        res.components_code = dict(parts)
        res.note = ("集成验证通过" if res.verified else "仍未通过: " + err[:120])
        return res

    def _gen_one(self, requirement: str, spec: ComponentSpec,
                 prior: str, repair_hint: str = "") -> str:
        try:
            return self.gen.generate(requirement, spec, prior, repair_hint=repair_hint) or ""
        except Exception as e:  # noqa: BLE001
            return "# generate failed for %s: %s\n" % (spec.name, e)


def build_complex(requirement: str, generator: Generator,
                  max_repair: int = MAX_REPAIR) -> dict[str, Any]:
    """复杂场景代码生成入口。"""
    return ComplexCodegen(generator, max_repair=max_repair).build(requirement).as_dict()


# ────────────────────────────── 生产实现:包 LLM ──────────────────────────────
import json as _json


def _extract_json(text: str) -> dict:
    for pat in (r"```json\s*(\{.*?\})\s*```", r"```\s*(\{.*?\})\s*```", r"(\{.*\})"):
        m = re.search(pat, text or "", re.DOTALL)
        if m:
            try:
                obj = _json.loads(m.group(1))
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return {}


def _strip_code(text: str) -> str:
    """从 LLM 回复抠出纯代码块。"""
    m = re.search(r"```(?:python|py)?\s*(.*?)```", text or "", re.DOTALL)
    return (m.group(1) if m else (text or "")).strip()


class LlmGenerator:
    """生产生成器:用 LLM 拆解需求 + 逐组件生成。

    llm 需有 ``chat(messages, model_key=..., max_tokens=..., temperature=...) -> resp(.text)``
    (与 devbot LlmClient 一致);可选传 codedoc 客户端做接地检索(复用 codegen_agent 的 retrieve)。
    """

    def __init__(self, llm: Any, codedoc: Any = None):
        self.llm = llm
        self.codedoc = codedoc

    def _chat(self, system: str, user: str, max_tokens: int = 1400) -> str:
        resp = self.llm.chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model_key="codegen", max_tokens=max_tokens, temperature=0.2)
        return getattr(resp, "text", "") or ""

    def plan(self, requirement: str) -> Plan:
        system = (
            "你是资深架构师。把编码需求拆成最小可独立生成的组件(函数/类),给出彼此依赖,"
            "并写一段只用 assert 的集成检查代码(import solution 里各组件,验证它们协作正确)。\n"
            "只输出 JSON:\n"
            '{"components":[{"name":"...","kind":"function|class","description":"...",'
            '"depends_on":["..."]}],"check_code":"<纯 assert 的 python>"}')
        data = _extract_json(self._chat(system, "需求:\n" + requirement, max_tokens=1200))
        comps = []
        for c in data.get("components", []) or []:
            if isinstance(c, dict) and c.get("name"):
                comps.append(ComponentSpec(
                    name=str(c["name"]), kind=str(c.get("kind", "function")),
                    description=str(c.get("description", "")),
                    depends_on=[str(d) for d in (c.get("depends_on") or [])]))
        return Plan(components=comps, check_code=str(data.get("check_code", "") or ""))

    def generate(self, requirement: str, spec: ComponentSpec,
                 prior_code: str, repair_hint: str = "") -> str:
        grounding = ""
        if self.codedoc is not None:                      # 可选:codedoc 接地取真实 API
            try:
                hits = self.codedoc.search("", "%s %s" % (spec.name, spec.description), top_k=4) or []
                grounding = "\n".join("- %s%s" % (h.get("qualified_name", ""), h.get("signature", ""))
                                      for h in hits[:4])
            except Exception:
                grounding = ""
        system = (
            "你是资深工程师。只生成**这一个组件**的 Python 代码(顶层定义,可被 import)。"
            "复用下方已生成组件里的真实符号,不要重定义它们。只输出 ```python 代码块。")
        user = "需求:%s\n\n要生成的组件:%s (%s) — %s\n依赖:%s" % (
            requirement, spec.name, spec.kind, spec.description, spec.depends_on)
        if prior_code:
            user += "\n\n# 已生成的组件(可直接调用,勿重定义)\n```python\n%s\n```" % prior_code[:3000]
        if grounding:
            user += "\n\n# codedoc 检索到的真实 API(优先复用)\n" + grounding
        if repair_hint:
            user += "\n\n# 上次失败,修正后重出本组件\n" + repair_hint
        return _strip_code(self._chat(system, user))
