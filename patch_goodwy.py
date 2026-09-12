#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Goodwy/Messages (Right Messages) 定制补丁脚本

用法:
  python3 patch_goodwy.py commons <Goodwy-Commons仓库根目录> --date-format "M-d-yyyy" --date-mode full
  python3 patch_goodwy.py app     <Messages仓库根目录>       --date-format "M-d-yyyy" --commons-version 99.0.0-custom

做的事:
  1. 时间统一: 所有日期格式常量 -> 指定格式; dateFormat 强制读取, 不受旧数据/Locale 影响;
               列表里"今天只显示时间""今年不显示年份"的逻辑全部去掉
  2. 去掉主界面/设置页的"关于"入口, 并把"关于页"空壳化(里面所有选项一并消失)
  3. 语言只保留 简体中文(中国)
  4. 应用内所有斜体 (android:textStyle="italic" / Typeface.ITALIC) 改成默认正体

设计原则: 匹配不上只 warn 不 exit(1), 云编译不会因为某一处正则失效就整轮失败。
"""

import argparse
import os
import re
import shutil
import sys

# ------------------------------------------------------------------ 基础工具
def log(msg):
    print("[patch] %s" % msg, flush=True)


def warn(msg):
    print("[patch][WARN] %s" % msg, flush=True)


def read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def rel(root, p):
    return os.path.relpath(p, root)


def walk_files(root, exts):
    for dp, dn, fn in os.walk(root):
        parts = dp.replace("\\", "/").split("/")
        if any(x in (".git", "build", ".gradle", ".idea") for x in parts):
            continue
        for f in fn:
            if f.endswith(exts):
                yield os.path.join(dp, f)


def find_file(root, subpath, filename):
    """优先按预期路径找, 找不到再全仓搜索(按文件名 + 路径片段)"""
    direct = os.path.join(root, subpath, filename)
    if os.path.exists(direct):
        return direct
    tail = subpath.rstrip("/").split("/")[-1]
    for p in walk_files(root, (".kt",)):
        if os.path.basename(p) == filename and tail in p.replace("\\", "/"):
            return p
    for p in walk_files(root, (".kt",)):
        if os.path.basename(p) == filename:
            return p
    return None


MODIFIERS = (r"(?:(?:private|internal|override|public|protected|open|suspend|"
            r"inline|operator|abstract|final|actual|expect|tailrec|external|"
            r"infix|internal)\s+)*")


def _mask(src):
    """把注释和字符串内容替换成等长空格, 只留代码骨架。
    定位函数体时必须用它 —— Long.kt 里就有一段被注释掉的旧实现,
    签名和真的一模一样, 直接 re.search 会命中注释, 后面的大括号配平全乱。

    用正则实现而非逐字符循环: commons 有几百个 kt 文件、数 MB 源码,
    Python 逐字符跑一遍要几十秒, CI 里会被拖成超时。
    """
    def blank(m):
        return re.sub(r'[^\n]', ' ', m.group(0))

    pat = re.compile(
        r'/\*.*?\*/'                  # /* ... */
        r'|//[^\n]*'                   # // ...
        r'|"""(?:\\.|[^\\])*?"""'        # """ ... """
        r'|"(?:\\.|[^"\\\n])*"'          # " ... "
        r"|'(?:\\.|[^'\\\n])*'",         # ' ... '
        re.S)
    return pat.sub(blank, src)


def _anchored(sig_pattern):
    """给签名正则加上"行首 + 修饰符"前缀。已加过就原样返回 —— 必须幂等。

    幂等这条不能省: _stub_fun 会先自己加前缀做"块体/表达式体"判断,
    再把加过前缀的 pattern 交给 replace_fun_body -> fun_span。如果 fun_span
    认不出它已经加过, 就会再拼一遍, 于是正则里出现第二个 (?m) 且不在开头:
      - Python 3.12: 直接 re.error "global flags not at the start", CI 当场挂
      - Python 3.10: 只给一条 DeprecationWarning, 本地跑得好好的
    这个"本地通过、CI 炸"的差异整整浪费了一轮构建, 所以幂等要写在这里。
    """
    if sig_pattern.startswith("^") or sig_pattern.startswith("(?m)"):
        return sig_pattern
    # 只允许行首到 fun 之间是空白和修饰符, 保证命中真函数而不是注释/字符串
    return r"(?m)^[ \t]*" + MODIFIERS + sig_pattern


def fun_span(src, sig_pattern):
    """按签名定位函数体: 返回 (body_start, body_end)。
    body_start 是 '{' 的下标, body_end 是配对的 '}' 的下标。"""
    sig_pattern = _anchored(sig_pattern)
    msk = _mask(src)
    m = re.search(sig_pattern, msk)
    if not m:
        return None
    # 签名模式自带 '{'(如 defaultConfig\s*\{) 时, 它本身就是块起始。
    # 若再去 m.end() 之后找, 会命中下一个嵌套块(如 ksp {), 大括号配平全错位。
    if sig_pattern.rstrip().endswith(r"\{"):
        i = msk.rfind("{", m.start(), m.end())
    else:
        i = msk.find("{", m.end())
    if i < 0:
        return None
    depth = 0
    j = i
    while j < len(msk):
        c = msk[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return (i, j)
        j += 1
    return None


def verify_syntax(root):
    """补丁跑完后自检: 掩码掉注释与字符串, 检查 {} () [] 是否配平。

    这道检查专治"把函数定义注释掉一半"这类灾难 —— 上一版就是这么把
    `fun Activity.showSideloadingDialog() {` 注释成 `fun Activity.// ... {`,
    编译期才炸, 浪费一整轮 CI。宁可多花几秒在这里报警。
    """
    bad = []
    n = 0
    for p in walk_files(root, (".kt", ".java", ".kts")):
        try:
            s = read(p)
        except Exception:
            continue
        n += 1
        m = _mask(s)
        for op, cl in (("{", "}"), ("(", ")"), ("[", "]")):
            d = m.count(op) - m.count(cl)
            if d != 0:
                bad.append("%s: '%s' 比 '%s' 多 %d 个" % (rel(root, p), op, cl, d))
                break
    if bad:
        warn("语法自检: %d/%d 个文件括号不配平, 构建大概率会失败:" % (len(bad), n))
        for b in bad[:10]:
            warn("  " + b)
        return False
    log("语法自检: %d 个 Kotlin/Gradle 文件括号全部配平" % n)
    return True


def replace_fun_body(src, sig_pattern, new_body):
    """替换整个函数体(保留签名与外层大括号)。返回 (new_src, ok)"""
    span = fun_span(src, sig_pattern)
    if not span:
        return src, False
    s, e = span
    return src[:s + 1] + new_body + src[e:], True


def drop_branch(src, cond):
    """删掉一个 if/else-if 分支(含条件与函数体), 用于彻底移除语音输入这类
    散布在多个方法里的条件分支。返回 (new_src, count)。

    两种形态都处理:
      if (COND) {                 -> 整块删掉(它是独立的 if, 没有 else 跟随)
        A
      }

      } else if (COND) {          -> 只删 "else if (COND) { A }" 这一段,
        A                            保留前后的 "} " 与 " else {", 变成
      } else {                       } else { B }
        B
      }

    用大括号配平定位, 不依赖固定行数 —— 分支内部嵌套再多 {} 也不会截错。
    """
    msk = _mask(src)
    rx = re.compile(r'(?m)^([ \t]*)(?:\} )?(else )?if \(' + cond + r'\) \{')
    plan = []
    for m in rx.finditer(msk):
        i = msk.rfind("{", m.start(), m.end())
        if i < 0:
            continue
        depth, j = 0, i
        while j < len(msk):
            if msk[j] == "{":
                depth += 1
            elif msk[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if depth != 0:
            continue
        if m.group(2):                      # "} else if (COND) { A }"
            # 只删 "else if (...) { A }" 这一段, 留下前面的 "} " 与后面的 " else {"
            plan.append((m.start() + m.group(0).index("else"), j + 1))
            continue
        # 独立的 if (COND) { A }: 后面若跟着 else 就不能只删 if 那段 ——
        # 删完会剩一个孤儿 ` else { ... }`, 语法直接崩。这种情况原样保留:
        # COND 已被抽空的实现置为 false, 运行时自然走 else 分支, 效果一样。
        rest = msk[j + 1:]
        k = 0
        while k < len(rest) and rest[k] in " \t\r\n":
            k += 1
        if rest[k:k + 4] == "else":
            continue
        plan.append((m.start(), j + 1))

    out = src
    for start, end in sorted(plan, reverse=True):
        out = out[:start] + out[end:]
    n = len(plan)
    if n:
        out = re.sub(r'\}[ \t]{2,}else', '} else', out)
        out = re.sub(r'\n[ \t]*\n[ \t]*\n', '\n\n', out)
    return out, n


def drop_fun(src, sig_pattern):
    """整段删除一个顶层函数(含签名与函数体), 用于移除波斯历分支函数。
    返回 (new_src, ok)。"""
    sig_pattern = _anchored(sig_pattern)
    msk = _mask(src)
    m = re.search(sig_pattern, msk)
    if not m:
        return src, False
    i = msk.rfind("{", m.start(), m.end()) if sig_pattern.rstrip().endswith(r"\{") \
        else msk.find("{", m.end())
    if i < 0:
        return src, False
    depth, j = 0, i
    while j < len(msk):
        if msk[j] == "{":
            depth += 1
        elif msk[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if depth != 0:
        return src, False
    end = j + 1
    while end < len(src) and src[end] in " \t":
        end += 1
    for _ in range(2):                      # 吃掉收尾空行, 不留空洞
        if end < len(src) and src[end] == "\n":
            end += 1
    return src[:m.start()] + src[end:], True


# ------------------------------------------------------------------ 1. 时间统一
def patch_sdk_align(root, sdk):
    """把 commons 的 compileSdk/targetSdk 对齐到 app 的值。

    背景(实测踩过): commons main 已经升到 AGP 9.3.1 / compileSdk 37 /
    lifecycle 2.11.0, 而 app 还在 AGP 9.0.1 / compileSdk 36。拿 main 编出来的
    AAR 会带 "requires compile against version 37" 的元数据, app 一解析就
    CheckAarMetadata 失败。

    优先解法是 clone 时严格 checkout app 锁定的那个 commons commit;
    这里是兜底 —— 万一有人手动把 commons_ref 改成 main 也能编过。
    注意: lifecycle 2.11.0 硬性要求 compileSdk >= 37, 所以降级 SDK 时必须
    同时把 lifecycle 压回 2.10.0, 否则报的是同一个错。
    """
    if not sdk:
        return
    p = os.path.join(root, "gradle", "libs.versions.toml")
    if not os.path.exists(p):
        warn("commons 侧找不到 gradle/libs.versions.toml, 跳过 SDK 对齐")
        return
    s = read(p)
    orig = s

    s, k1 = re.subn(r'(?m)^(app-build-compileSDKVersion\s*=\s*)"[^"]*"',
                    r'\1"%s"' % sdk, s)
    s, k2 = re.subn(r'(?m)^(app-build-targetSDK\s*=\s*)"[^"]*"',
                    r'\1"%s"' % sdk, s)

    s, _k3 = pin_lifecycle(s, sdk)
    if s != orig:
        write(p, s)
        log("commons: compileSdk/targetSdk 已对齐为 %s" % sdk)


def pin_lifecycle(s, sdk):
    """compileSdk < 37 时把 androidx-lifecycle 压回 2.10.0。纯函数, 不落盘。

    必须做成可单独调用的一步, 而不是嵌在 patch_sdk_align 里:
    patch_android16 跑在 patch_sdk_align 之后(它负责最终拍板 SDK 数值),
    若 --compile-sdk 传进来的是 37, patch_sdk_align 会跳过降级(37 不需要压),
    接着 patch_android16 把数值改成 36, 于是留下 lifecycle 2.11.0 +
    compileSdk 36 的组合 —— 正是 CheckAarMetadata 失败的那个组合。
    所以拍板完 SDK 之后必须再压一次, 而不是只压一次。
    """
    if int(sdk) >= 37:
        return s, 0
    s2, k = re.subn(r'(?m)^(androidx-lifecycle\s*=\s*)"[^"]*"',
                    r'\1"2.10.0"', s)
    # 只在真变了时才打日志: patch_sdk_align 和 patch_android16 都会调它,
    # 第二次进来时值已经是 2.10.0, 再报一次"已压回"纯属噪音。
    if k and s2 != s:
        log("commons: androidx-lifecycle 压回 2.10.0 (2.11.0 要求 compileSdk>=37)")
    return s2, k


# --------------------------------------------- Android 16 (API 36) 定向优化
ANDROID_16_SDK = "36"

# min / target / compile 三档在 gradle/libs.versions.toml 里的键名
SDK_KEYS = ("app-build-compileSDKVersion", "app-build-targetSDK",
            "app-build-minimumSDK")

# KSP 2.3.5 有个已知 bug: 处理结束后 IntelliJ PSI 的后台清理线程(AWT-EventQueue-0)
# 在 Application 已销毁后仍去 getService(), 抛 NPE。抛在 AWT 线程而非构建主线程,
# Gradle 捕获不到, 所以不影响产物 —— 官方 issue 标题即 "does not break build"
# (google/ksp#2763)。但日志里一大片红字, 且掩盖真正的编译错误。
# 官方已在 2.3.6 修掉, release note 原文: "Fixed a KSP version 2.3.5 CI error
# exception that does not break build checks (#2763)"。
# KSP 版本必须与 Kotlin 主版本对齐(2.3.x 对 2.3.x), 本项目 Kotlin 2.3.10,
# 所以 2.3.5 -> 2.3.6 是同线小版本, 不会引入 Kotlin 不匹配。
KSP_MIN_OK = "2.3.6"

# META-INF 里几样 release 用不到的东西。
# 说明白收益: DebugProbesKt.bin 是 kotlinx-coroutines 的调试探针, 只有
# DebugProbes.install() 那种 IDE 协程调试才需要, 撑死 1~2 KB; 许可证文本同理。
# 所以别指望靠它瘦身 —— 加它纯粹是因为"白给且零风险", 真正的体积大头在
# classes.dex 和 resources.arsc, 得看体积诊断才知道。
# 不删 .kotlin_module: 那是 Kotlin 模块元数据, 某些反射/序列化场景会读。
PACKAGING_RES = (
    "        resources {\n"
    "            // 协程调试探针: kotlinx-coroutines 自带, 只有 IDE 协程调试用到。\n"
    "            // 路径必须是裸文件名 —— 它落在 APK 根目录(官方 README 与\n"
    "            // issue #2274 都确认), 早先写成 /META-INF/ 前缀等于没排除,\n"
    "            // 于是这文件一直躺在 APK 里。官方原话: exclude it at no loss\n"
    "            // of functionality, 排除它没有任何功能损失。\n"
    "            excludes += \"DebugProbesKt.bin\"\n"
    "            // 许可证文本: release 用不到\n"
    "            excludes += \"/META-INF/{AL2.0,LGPL2.1}\"\n"
    "        }\n"
)

# Android 16 起 Google Play 强制支持 16KB 内存页: so 必须页对齐且不压缩。
# AGP 需要显式关掉 legacy packaging(默认压缩 so)才会按 16KB 对齐打包。
PACKAGING_16KB = (
    "    packaging {\n" +
    PACKAGING_RES +
    "        jniLibs {\n"
    "            // Android 16: 16KB 页大小要求 so 不压缩且页对齐\n"
    "            useLegacyPackaging = false\n"
    "        }\n"
    "    }\n"
)


def _ver_tuple(v):
    """把 "2.3.5" 变成 (2, 3, 5) 以便比较。非数字段按 0 处理。"""
    parts = []
    for seg in re.split(r'[.\-]', v):
        parts.append(int(seg) if seg.isdigit() else 0)
    return tuple(parts) or (0,)


def patch_ksp_version(s):
    """把 KSP 升到 2.3.6 以上, 消除 #2763 的 AWT NPE 噪音。纯函数, 不落盘。

    只在"当前版本更低"时才升, 不反向降级 —— 上游哪天自己升到 2.4.x 了,
    这里不能把人拉回 2.3.6。
    """
    m = re.search(r'(?m)^ksp\s*=\s*"([^"]*)"', s)
    if not m:
        return s, 0          # commons 侧可能压根没有 ksp, 正常
    cur = m.group(1)
    if _ver_tuple(cur) >= _ver_tuple(KSP_MIN_OK):
        log("KSP 已是 %s (>= %s), 无需升级" % (cur, KSP_MIN_OK))
        return s, 0
    s2, k = re.subn(r'(?m)^(ksp\s*=\s*)"[^"]*"', r'\1"%s"' % KSP_MIN_OK, s)
    if k:
        log("KSP %s -> %s (修掉 #2763: AWT 后台线程的 NPE, 构建日志里那片红字)"
            % (cur, KSP_MIN_OK))
    return s2, k


def patch_android16(root, sdk=ANDROID_16_SDK):
    """把 min / target / compile 三档 SDK 全部钉到 Android 16 (API 36)。

    为什么值得单独做这一步:
      - minSdk 26 -> 36 后, R8 能确定 `Build.VERSION.SDK_INT >= XX` 全是常量,
        所有为 Android 8~15 写的兼容分支会被整体剪掉 —— APK 更小、运行更快,
        而且这是编译器自动做的, 不需要(也不应该)手工去删那些 if。
      - target/compile 上游已经是 36, 这一步主要是兜底: 万一上游升到 37,
        会把 lifecycle 等依赖一起拖到要求 compileSdk>=37, app 侧解析 AAR 就
        会 CheckAarMetadata 失败(这个坑之前踩过), 钉死 36 可避免。
      - commons 的 minSdk 不能高于 app, 否则 manifest merger 报错, 所以两侧
        都钉同一个值最省事。

    代价: 装不上 Android 16 以下的设备。使用者只跑 Android 16, 无需兼容。
    """
    n = 0
    p = os.path.join(root, "gradle", "libs.versions.toml")
    if not os.path.exists(p):
        warn("找不到 gradle/libs.versions.toml, 跳过 Android 16 定向优化")
        return
    s = read(p)
    orig = s
    for key in SDK_KEYS:
        s, k = re.subn(r'(?m)^(%s\s*=\s*)"[^"]*"' % re.escape(key),
                       r'\1"%s"' % sdk, s)
        if not k:
            warn("libs.versions.toml 里没有 %s, 请确认键名" % key)
        n += k
    # 钉完最终数值后再压一次 lifecycle: 若 --compile-sdk 传的是 37,
    # patch_sdk_align 那步会跳过降级, 而这里刚把 compileSdk 改成 36,
    # 不补这一刀就会留下 "lifecycle 2.11.0 + compileSdk 36" 的致命组合。
    s, _kl = pin_lifecycle(s, sdk)
    s, _kk = patch_ksp_version(s)
    if s != orig:
        write(p, s)
        log("Android 16: min/target/compile 三档 SDK 已全部钉为 %s (%d 处)" % (sdk, n))

    # 16KB 页大小只有最终打 APK 的 app 模块需要; commons 是 library, 没有 app 目录
    gb = os.path.join(root, "app", "build.gradle.kts")
    if not os.path.exists(gb):
        return
    s = read(gb)
    # 先补 resources 排除(幂等)。放在"已有配置就 return"之前, 否则
    # 第二次跑进来会因为 useLegacyPackaging 已存在而直接跳过, 永远补不上。
    s, added_res = _ensure_packaging_res(s)
    if added_res:
        log("build.gradle.kts: 已补上 META-INF 资源排除")
    if "useLegacyPackaging" in s:
        log("build.gradle.kts: 已有 16KB 打包配置, 跳过")
        write(gb, s)
        return
    span = fun_span(s, r'android\s*\{')
    if not span:
        warn("build.gradle.kts: 未定位到 android 块, 跳过 16KB 页大小配置")
        if s != read(gb):
            write(gb, s)
        return
    j = span[1]
    head = s[:j]
    ins = PACKAGING_16KB if head.endswith("\n") else "\n" + PACKAGING_16KB
    write(gb, head + ins + s[j:])
    log("build.gradle.kts: 已启用 16KB 页大小对齐 (Android 16 强制要求)")


def _ensure_packaging_res(s):
    """已有 packaging 块时补上 resources 排除段; 没有 packaging 块就原样返回。

    为什么要单独处理"已有块"的情况: packaging 块可能早就存在(比如上游自带
    jniLibs 配置), 那时不能整块重插, 只能往里补一段。返回 (new_src, changed)。
    """
    span = fun_span(s, r'packaging\s*\{')
    if not span:
        return s, False
    i = span[0]                      # '{' 的下标
    body = s[i:span[1]]
    if 'resources' in body:
        return s, False
    return s[:i + 1] + "\n" + PACKAGING_RES + s[i + 1:], True


def patch_ispro_always_true(root):
    """把 Context.isPro() 直接改成 true —— 这是解锁所有付费 UI 最关键的一刀。

    为什么比逐处改 if 判断强得多:
      SettingsActivity 里有 alpha = if (pro) 1f else 0.4f(未解锁时变灰)、
      addLockedLabelIfNeeded(未解锁时在标题后加"(已锁定)")、
      if (isPro()) { 可点 } 等一大堆分支;
      commons 的 CustomizationActivity 里 isProVersion() 也有十几个锁定分支。
      isPro() 恒 true 后这些自动全部走解锁路径, 一行都不用改,
      也不可能出现"改漏某一处"的情况。

    注意 isProNoGP 仍要单独改 true(见 patch_baseconfig):
      PurchaseActivity 里 proSwitch.isChecked 读的是 baseConfig.isProNoGP,
      不是 isPro()。两个都要改, 否则开关显示还是关闭。
    """
    p = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/extensions",
                  "Context.kt")
    if not p:
        warn("找不到 commons Context.kt, 跳过 isPro 一刀切")
        return
    src = read(p)
    if "fun Context.isPro() = true" in src:
        log("commons: isPro() 已处理过, 跳过")
        return
    # isPro() 是表达式体函数(没有 {}), 一路延伸到下一个顶层声明之前
    new, k = re.subn(
        r'(?ms)^fun Context\.isPro\(\) =.*?(?=\n\S|\Z)',
        'fun Context.isPro() = true  // 付费功能已全部解锁', src, count=1)
    if k:
        write(p, new)
        log("commons: Context.isPro() 恒为 true "
            "(锁定标签/变灰/自定义颜色等限制全部解除)")
    else:
        warn("commons: 未匹配到 Context.isPro() 定义")


def patch_purchase_page(root):
    """foss 渠道的 PurchaseActivity(项目支持页) 空壳化。

    前提: 入口已全隐藏(购买卡片 + 小费罐), 且 isPro() 恒 true 不需要购买,
    这个页面已经是死页面。空壳化后即使被残留调用也只是瞬间关闭, 不会崩。
    想保留它(比如为了目视确认 UNLOCK 是开着的)就加 --keep-purchase-page。
    """
    p = os.path.join(root, "commons", "src", "foss", "kotlin",
                     "com", "goodwy", "commons", "activities",
                     "PurchaseActivity.kt")
    if not os.path.exists(p):
        hits = [q for q in walk_files(root, (".kt",))
                if os.path.basename(q) == "PurchaseActivity.kt"]
        if not hits:
            log("commons: 未找到 foss PurchaseActivity, 跳过")
            return
        p = hits[0]
    src = read(p)
    if "购买页已精简" in src:
        log("commons: PurchaseActivity 已处理过, 跳过")
        return
    new, ok = replace_fun_body(
        src, r'override fun onCreate\(savedInstanceState: Bundle\?\)',
        '\n        super.onCreate(savedInstanceState)\n'
        '        finish()  // 购买页已精简\n    ')
    if ok:
        write(p, new)
        log("commons: foss PurchaseActivity 已空壳化 (进入即关闭)")
    else:
        warn("commons: PurchaseActivity.onCreate 未匹配")


def patch_constants(root, fmt):
    """把所有 DATE_FORMAT_XXX 常量统一成一个格式。
    Goodwy 的 commons 有 14 个常量(ONE..FOURTEEN), 不是 Fossify 的 8 个。"""
    path = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/helpers",
                     "Constants.kt")
    if not path:
        warn("找不到 Constants.kt, 跳过常量替换")
        return 0
    src = read(path)
    new, k = re.subn(r'(const val DATE_FORMAT_[A-Z0-9_]+\s*=\s*)"[^"]*"',
                     lambda m: '%s"%s"' % (m.group(1), fmt), src)
    if k:
        write(path, new)
    log("Constants.kt: 已统一 %d 个日期格式常量 -> %s" % (k, fmt))
    if k < 8:
        warn("只替换了 %d 个常量, 请检查 %s (预期 >= 8)" % (k, rel(root, path)))
    return k


def patch_baseconfig(root, fmt, a_unlock_pro=True):
    """dateFormat 强制返回固定格式, 不再读 SharedPreferences / 系统 Locale"""
    path = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/helpers",
                     "BaseConfig.kt")
    if not path:
        warn("找不到 BaseConfig.kt, 跳过")
        return
    src = read(path)
    orig = src

    # 1) getter 写死
    src, k1 = re.subn(r'(var dateFormat: String\s*\n\s*get\(\)\s*=\s*)[^\n]*',
                      lambda m: m.group(1) + '"%s"' % fmt, src)

    # 2) getDefaultDateFormat() 整个函数体 -> ONE (双保险)
    src, k2 = replace_fun_body(
        src, r'fun getDefaultDateFormat\(\)',
        '\n        return DATE_FORMAT_ONE\n    ')

    # 3) 波斯历(Shamsi)总开关写死 false。
    #    这是删波斯历最稳的一刀: 所有 context.baseConfig.useShamsi 恒为 false,
    #    运行时永远走公历分支, 且不破坏任何赋值点(仍是 prefs 写入)。
    src, k3 = re.subn(r'(var useShamsi: Boolean[^\n]*\n\s*get\(\)\s*=\s*)[^\n]*',
                      r'\1false', src)

    # 4) 解锁全部付费功能 (项目支持页的 UNLOCK 开关默认打开)。
    #    foss 渠道的 Context.isPro() 直接取 baseConfig.isProNoGP,
    #    写死 true 后一举三得:
    #      - PurchaseActivity 的 pro_switch 默认勾选
    #      - 自定义颜色等付费功能全部解锁
    #      - 设置页购买卡片 beGoneIf(isPro()) 自动隐藏
    #    setter 保留(还是往 prefs 写), 不影响任何赋值点。
    if a_unlock_pro:
        src, k4 = re.subn(
            r'(var isProNoGP: Boolean\s*\n\s*get\(\)\s*=\s*)[^\n]*',
            r'\1true', src)
    else:
        k4 = 0

    # 5) 签名/侧载认证: appSideloadingStatus 恒为 FALSE。
    #    这是"应用已损坏, 请从商店重新下载"弹窗的源头开关。
    src, k5 = re.subn(
        r'(var appSideloadingStatus: Int\s*\n\s*get\(\)\s*=\s*)[^\n]*',
        r'\1SIDELOADING_FALSE', src)

    # 6) 应用主题默认「系统默认」(Material You) 而不是浅色。
    #    自定义外观页的当前主题由 getCurrentThemeId() 推导, 它第一个看的
    #    就是 isSystemThemeEnabled —— 官方把默认值写成 false(原本的 isSPlus()
    #    被注释掉了), 于是全新安装一律落到 THEME_LIGHT 浅色。
    #
    #    写死 true(而不是 isSPlus()): 目标机是 Android 16, 用不上低版本兼容。
    #    代价说清楚 —— setupThemes() 只在 isSPlus() 时 put(THEME_SYSTEM),
    #    所以在 Android 12 以下这台"系统默认"项不会出现在选项表里,
    #    getThemeText() 会退化显示「自定义」。不会崩, 只是低版本上这项无意义。
    #    另外预置默认值只在 prefs 里没写入过时才生效, 老用户升级不受影响。
    src, k6 = re.subn(
        r'(var isSystemThemeEnabled: Boolean\s*\n\s*get\(\)\s*=\s*'
        r'prefs\.getBoolean\(IS_SYSTEM_THEME_ENABLED,\s*)[^\n)]*',
        r'\1true', src)

    if src != orig:
        write(path, src)
    if k1:
        log("BaseConfig.kt: dateFormat 已强制为 \"%s\" (旧数据/系统 Locale 不再影响)" % fmt)
    else:
        warn("BaseConfig.kt: 未匹配到 dateFormat getter, 需要人工确认")
    if k2:
        log("BaseConfig.kt: getDefaultDateFormat() -> DATE_FORMAT_ONE")
    if k3:
        log("BaseConfig.kt: useShamsi 已写死 false (波斯历总开关关闭)")
    else:
        warn("BaseConfig.kt: 未匹配到 useShamsi, 波斯历可能仍可开启")
    if k4:
        log("BaseConfig.kt: isProNoGP 恒为 true (项目支持 UNLOCK 默认打开, 付费功能全解锁)")
    else:
        warn("BaseConfig.kt: 未匹配到 isProNoGP, 项目支持开关可能仍是关闭")
    if k5:
        log("BaseConfig.kt: appSideloadingStatus 恒为 SIDELOADING_FALSE (签名认证弹窗关闭)")
    else:
        warn("BaseConfig.kt: 未匹配到 appSideloadingStatus")
    if k6:
        log("BaseConfig.kt: isSystemThemeEnabled 默认 true (应用主题默认跟随系统, "
            "而非浅色)")
    else:
        warn("BaseConfig.kt: 未匹配到 isSystemThemeEnabled, 应用主题默认仍是浅色")


FULL_BODY = '''
    val useDateFormat = dateFormat ?: context.baseConfig.dateFormat
    val useTimeFormat = timeFormat ?: context.getTimeFormat()

    val cal = Calendar.getInstance(Locale.ENGLISH)
    cal.timeInMillis = this

    var format = useDateFormat
    if (!hideTimeOnOtherDays) {
        format = "$format, $useTimeFormat"
    }

    // full 模式: 今天也显示日期, 年份一律保留
    return DateFormat.format(format, cal).toString()
'''

KEEP_YEAR_BODY = '''
    val useDateFormat = dateFormat ?: context.baseConfig.dateFormat
    val useTimeFormat = timeFormat ?: context.getTimeFormat()

    val cal = Calendar.getInstance(Locale.ENGLISH)
    cal.timeInMillis = this

    var format = useDateFormat
    if (!hideTimeOnOtherDays) {
        format = "$format, $useTimeFormat"
    }

    // keep-year 模式: 今天仍显示时间, 但其它日期不再剥掉年份
    return if (hideTodaysDate && DateUtils.isToday(this)) {
        DateFormat.format(useTimeFormat, cal).toString()
    } else {
        DateFormat.format(format, cal).toString()
    }
'''


def patch_longkt(root, mode):
    """重写 formatDateOrTime: 这才是决定会话列表里日期长什么样的入口函数。
    Goodwy 会把它分派到 Gregorian / Shamsi 两个私有实现, 直接在顶层拦截最干净。"""
    path = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/extensions",
                     "Long.kt")
    if not path:
        warn("找不到 Long.kt, 跳过 (日期显示多半不会变)")
        return
    src = read(path)
    body = FULL_BODY if mode == "full" else KEEP_YEAR_BODY

    new, ok = replace_fun_body(src, r'fun Long\.formatDateOrTime\(', body)
    if not ok:
        warn("Long.kt: 未定位到 formatDateOrTime(), 需人工处理 %s" % rel(root, path))
        return
    write(path, new)
    log("Long.kt: formatDateOrTime 已重写 (mode=%s) —— 不再隐藏年份, "
        "%s" % (mode, "今天也显示日期" if mode == "full" else "今天仍显示时间"))

    # 兜一层: Gregorian 私有实现也改掉, 防止有人绕过顶层函数
    greg = '''
    val cal = Calendar.getInstance(Locale.ENGLISH)
    cal.timeInMillis = this

    return if (hideTodaysDate && DateUtils.isToday(this)) {
        DateFormat.format(%s, cal).toString()
    } else {
        var format = dateFormat
        if (!hideTimeOnOtherDays) {
            format = "$format, $timeFormat"
        }
        DateFormat.format(format, cal).toString()
    }
''' % ("dateFormat" if mode == "full" else "timeFormat")
    new2, ok2 = replace_fun_body(new, r'private fun Long\.formatDateOrTimeGregorian\(', greg)
    if ok2:
        write(path, new2)
        log("Long.kt: formatDateOrTimeGregorian 同步改写")

    # ---- 删波斯历(Shamsi) ----------------------------------------------
    # Goodwy 比 Fossify 多一套波斯历: formatDate / formatDateOrTime / toDayCode
    # 三个入口都有 isUseShamsi 分支, 分别落到 formatWithShamsiAdvanced /
    # formatDateOrTimeShamsi / toDayCodeShamsi。入口全改成无分支后, 这三个
    # private 函数就没人调用了, 可以整段删掉。
    src = read(path)

    src, ok3 = replace_fun_body(src, r'fun Long\.formatDate\(', SHAMSI_FREE_DATE)
    if ok3:
        log("Long.kt: formatDate 已去掉波斯历分支")
    else:
        warn("Long.kt: 未定位到 formatDate(), 波斯历分支可能残留")

    src, ok4 = replace_fun_body(src, r'fun Long\.toDayCode\(',
                                '\n    return toDayCodeGregorian(format)\n')
    if ok4:
        log("Long.kt: toDayCode 已去掉波斯历分支")
    else:
        warn("Long.kt: 未定位到 toDayCode()")

    n_drop = 0
    for sig in (r'private fun Long\.formatWithShamsiAdvanced\(',
                r'private fun Long\.formatDateOrTimeShamsi\(',
                r'fun Long\.toDayCodeShamsi\('):
        src, dropped = drop_fun(src, sig)
        n_drop += 1 if dropped else 0
    if n_drop:
        log("Long.kt: 已删除 %d 个波斯历私有函数" % n_drop)
    else:
        warn("Long.kt: 波斯历函数一个都没删掉, 请检查签名是否变了")

    # PersianDate import 没人用了就删, 否则留着只是 unused warning
    imp = "import saman.zamani.persiandate.PersianDate\n"
    if imp in src and src.replace(imp, "").count("PersianDate") == 0:
        src = src.replace(imp, "")
        log("Long.kt: 已移除 PersianDate import")

    write(path, src)
    # 形参 useShamsi 必须留着(调用方可能传命名参数), 不算残留;
    # 只统计"真被调用"的地方: 函数引用 / PersianDate
    left = [l.strip() for l in src.split("\n")
            if re.search(r'Shamsi\(|toDayCodeShamsi|formatWithShamsiAdvanced|PersianDate', l)
            and not l.strip().startswith("//")]
    if left:
        warn("Long.kt 仍残留 %d 处波斯历调用: %s" % (len(left), left[:3]))
    else:
        log("Long.kt: 波斯历清理完成 (仅保留形参占位以兼容调用方)")


SHAMSI_FREE_DATE = '''
    val useDateFormat = dateFormat ?: context.baseConfig.dateFormat
    val useTimeFormat = timeFormat ?: context.getTimeFormat()

    val isUseRelativeDate =
        useRelativeDate && (System.currentTimeMillis() - this <= transitionResolution)
    return if (isUseRelativeDate) {
        DateUtils.getRelativeDateTimeString(
            context,
            this,
            1.minutes.inWholeMilliseconds,
            transitionResolution,
            0
        ).toString()
    } else {
        // 波斯历(Shamsi)已移除: 一律按公历格式化
        formatWithGregorian(useDateFormat, useTimeFormat)
    }
'''


def patch_dialog(root):
    """日期格式弹窗: 去掉波斯历开关。

    重要教训(实测踩过):
      1) 这个文件只 import 了 beVisibleIf, 没有 beGone。插 beGone() 会
         Unresolved reference。所以隐藏统一用已 import 的 beVisibleIf(false)。
      2) 弹窗实际只有 8 个 Radio(One~Eight + Ten, 跳过 Nine), 但常量有 14 个。
         按常量名去生成 RadioNine/RadioEleven... 会 Unresolved。
         所以绝不按常量名猜布局里有哪些 id —— 需要时先扫布局文件。
      3) 日期常量已经全仓统一, 弹窗里所有选项显示的是同一个格式,
         选哪个结果都一样, 无需隐藏 Radio(隐藏反而有风险)。
    """
    path = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/dialogs",
                     "ChangeDateTimeFormatDialog.kt")
    if not path:
        warn("找不到 ChangeDateTimeFormatDialog.kt, 跳过")
        return
    src = read(path)
    orig = src

    # 波斯历开关: 用已 import 的 beVisibleIf(false) 隐藏, 不引入 beGone
    src = re.sub(r'changeDateTimeDialogUseShamsiHolder\.beVisibleIf\([^)]*\)',
                 'changeDateTimeDialogUseShamsiHolder.beVisibleIf(false)', src)
    # 取值也恒为 false, 彻底不走波斯历
    src = re.sub(r'(val useShamsi\s*=\s*)[^\n]*',
                 r'\1false', src)
    # Compose 版: 只留第一项
    src = re.sub(r'\n[ \t]*Pair\(DATE_FORMAT_(?!ONE\b)[A-Z0-9_]+,.*?\),(?=\n)', '', src)

    if src != orig:
        write(path, src)
        log("ChangeDateTimeFormatDialog.kt: 波斯历开关已移除 "
            "(日期常量已统一, 弹窗各选项显示同一格式)")


def patch_app_force_format(root, fmt):
    """兜底: App.kt 启动时硬写 dateFormat, 即使本地 commons 没生效也管用"""
    path = find_file(root, "app/src/main/kotlin/com/goodwy/smsmessenger", "App.kt")
    if not path:
        warn("找不到 App.kt, 跳过启动强制格式")
        return
    src = read(path)
    if "baseConfig.dateFormat" in src:
        log("App.kt: 已有 dateFormat 赋值, 跳过")
        return

    if "import com.goodwy.commons.extensions.baseConfig" not in src:
        m = re.search(r'^package\s+\S+\n', src, re.M)
        imp = "import com.goodwy.commons.extensions.baseConfig\n"
        src = (src[:m.end()] + "\n" + imp + src[m.end():]) if m else imp + src

    m = re.search(r'(override fun onCreate\(\)\s*\{\n)(\s*)(super\.onCreate\(\)\n)', src)
    if m:
        ins = '%sbaseConfig.dateFormat = "%s"\n' % (m.group(2), fmt)
        src = src[:m.end()] + ins + src[m.end():]
        write(path, src)
        log('App.kt: 启动时强制 baseConfig.dateFormat = "%s"' % fmt)
    else:
        warn('App.kt 未匹配到 onCreate, 需手动加 baseConfig.dateFormat = "%s"' % fmt)


def patch_threadactivity(root):
    """定时短信按钮: 本年内那条走系统 DateUtils.formatDateTime(FORMAT_NO_YEAR),
    完全不看 dateFormat, 是全局唯一的漏网之鱼 -> 统一成 formatDate"""
    path = find_file(root, "app/src/main/kotlin/com/goodwy/smsmessenger/activities",
                     "ThreadActivity.kt")
    if not path:
        warn("找不到 ThreadActivity.kt, 跳过定时短信时间统一")
        return
    src = read(path)
    if "scheduledMessageButton.text" not in src:
        warn("ThreadActivity.kt 里没有 scheduledMessageButton, 跳过")
        return

    pat = re.compile(
        r'(binding\.messageHolder\.scheduledMessageButton\.text\s*=\s*)'
        r'if\s*\(.*?\)\s*\{.*?\}\s*else\s*\{.*?\}\n', re.S)
    new, k = pat.subn(r'\1millis.formatDate(this)\n', src, count=1)
    if not k:
        warn("定时短信按钮代码没匹配上, 需人工改 (搜索 scheduledMessageButton)")
        return

    if "import com.goodwy.commons.extensions.formatDate" not in new:
        m = re.search(r'^import com\.goodwy\.commons\.extensions\.formatDateOrTime\n',
                      new, re.M)
        if m:
            new = new[:m.end()] + "import com.goodwy.commons.extensions.formatDate\n" + new[m.end():]
        else:
            m2 = re.search(r'^package\s+\S+\n', new, re.M)
            new = (new[:m2.end()] + "\nimport com.goodwy.commons.extensions.formatDate\n"
                   + new[m2.end():]) if m2 else \
                  "import com.goodwy.commons.extensions.formatDate\n" + new
    write(path, new)
    log("ThreadActivity.kt: 定时短信按钮已统一为 dateFormat "
        "(原走系统 DateUtils, FORMAT_NO_YEAR 会隐藏年份)")


# ------------------------------------------------------------------ 2. 去掉"关于"
def patch_menu_about(root):
    """主界面菜单里的「关于」: 不删 <item>(否则 R.id.about 消失会让 Kotlin 编译失败),
    改成 android:visible="false", 再把 MainActivity 里的分支删掉。"""
    p = os.path.join(root, "app", "src", "main", "res", "menu", "menu_main.xml")
    if os.path.exists(p):
        s = read(p)
        new = re.sub(r'(<item\b(?:(?!>).)*?@\+id/about(?:(?!>).)*?)(/?)>',
                     lambda m: (m.group(1) if 'android:visible' in m.group(1)
                                else m.group(1).rstrip() + ' android:visible="false"')
                     + m.group(2) + '>',
                     s, flags=re.S)
        if new != s:
            write(p, new)
            log("menu_main.xml: 主界面「关于」已设为不可见")
        else:
            warn("menu_main.xml 未匹配到 about 项")
    else:
        warn("找不到 menu_main.xml")

    p = find_file(root, "app/src/main/kotlin/com/goodwy/smsmessenger/activities",
                  "MainActivity.kt")
    if not p:
        warn("找不到 MainActivity.kt, 跳过分支清理")
        return
    src = read(p)
    new, k = re.subn(r'[ \t]*R\.id\.about\s*->\s*launchAbout\(\)\n', '', src)
    if k:
        write(p, new)
        log("MainActivity.kt: 已删除「关于」菜单分支 (%d 处)" % k)
    else:
        log("MainActivity.kt: 没有 R.id.about 分支, 无需清理")


def patch_settings_about(root):
    """设置页里的「关于」入口: 布局置 gone + setupAbout() 里 beGone() 双保险"""
    layout = os.path.join(root, "app", "src", "main", "res", "layout",
                          "activity_settings.xml")
    if os.path.exists(layout):
        s = read(layout)
        new, ok = set_view_gone(s, "settings_about_holder")
        if ok:
            write(layout, new)
            log("activity_settings.xml: 设置页「关于」已隐藏")
        else:
            warn("activity_settings.xml 里没找到 settings_about_holder")
    else:
        warn("找不到 activity_settings.xml")

    p = find_file(root, "app/src/main/kotlin/com/goodwy/smsmessenger/activities",
                  "SettingsActivity.kt")
    if not p:
        warn("找不到 SettingsActivity.kt, 跳过")
        return
    src = read(p)
    if "settingsAboutHolder.beGone()" in src:
        log("SettingsActivity.kt: 关于已处理过, 跳过")
        return
    m = re.search(r'(private fun setupAbout\(\)\s*=\s*binding\.apply\s*\{\n)', src)
    if m:
        src = src[:m.end()] + "        settingsAboutHolder.beGone()\n" + src[m.end():]
        write(p, src)
        log("SettingsActivity.kt: setupAbout() 已插入 settingsAboutHolder.beGone()")
    else:
        warn("SettingsActivity.kt 未匹配到 setupAbout(), 仅依赖布局隐藏")


def patch_start_about(root):
    """commons: startAboutActivity() 置空 —— 关于页根本不会被启动"""
    p = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/activities",
                  "BaseSimpleActivity.kt")
    if not p:
        warn("找不到 BaseSimpleActivity.kt, 跳过 startAboutActivity 置空")
        return
    src = read(p)
    new, ok = replace_fun_body(src, r'fun startAboutActivity\(',
                               '\n        return\n    ')
    if ok:
        write(p, new)
        log("commons: startAboutActivity() 已置空 (关于页无法被启动)")
    else:
        warn("commons: 未定位到 startAboutActivity()")


def patch_about_activity(root):
    """commons: AboutActivity.onCreate -> super + finish, 关于页里所有选项
    (FAQ / 邮件 / GitHub / 捐赠 / Patreon / 隐私政策 / 许可 / 贡献者) 一并失效"""
    p = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/activities",
                  "AboutActivity.kt")
    if not p:
        warn("找不到 AboutActivity.kt")
        return
    src = read(p)
    new, ok = replace_fun_body(
        src, r'override fun onCreate\(savedInstanceState: Bundle\?\)',
        '\n        super.onCreate(savedInstanceState)\n        finish()\n    ')
    if ok:
        write(p, new)
        log("commons: AboutActivity 已空壳化 (onCreate 直接 finish, 关于页所有选项彻底消失)")
    else:
        warn("commons: AboutActivity.onCreate 未匹配, 未空壳化")


# ------------------------------------------------------ 11. 语音输入(彻底删)
# 关键教训(实测踩过两轮):
#   上一版在 commons 里"整段删除" fun Activity.speechToText / isSpeechToTextAvailable,
#   再指望 app 侧把调用点一个个删干净。但 commons 和 app 是两次独立的 patch 运行
#   (commons 先跑并发布到 mavenLocal), commons 根本无从知道 app 里还有谁在用 ——
#   只要漏掉一个文件就是 Unresolved reference, 编译期才炸。MainActivity.kt 和
#   NewConversationActivity.kt 就是这么漏的。
#   所以改成"抽空实现"而不是"删除签名": 函数还在, 但 isSpeechToTextAvailable()
#   恒返回 false、speechToText() 什么都不做。app 侧任何漏网的调用点都能编译,
#   而且行为正确 —— 麦克风按钮 beVisibleIf(isSpeechToTextAvailable()) 自然是 gone。
SPEECH_STUBS = (
    (r'fun Activity\.isSpeechToTextAvailable\(',
     '\n    // 语音输入已移除: 一律报告"不可用"\n    return false\n'),
    (r'fun Activity\.speechToText\(',
     '\n    // 语音输入已移除: 空实现, 不再拉起语音识别界面\n'),
)


def _stub_fun(src, sig_pattern, body):
    """把函数体换成 inert 实现。只动"块体"函数, 表达式体一律跳过。

    为什么必须先判断函数体形态: fun_span 是"从签名末尾找第一个 '{'",
    遇到 `fun x() = someCall { ... }` 这种表达式体, 它会把后面某个不相干的
    块当成函数体, 整段替换掉 —— 属于改一个词毁一个文件的灾难。
    """
    sig_pattern = _anchored(sig_pattern)
    msk = _mask(src)
    m = re.search(sig_pattern, msk)
    if not m or msk[m.end() - 1] != "(":
        return src, False
    # 从签名里的 '(' 找到配对的 ')'
    d, j = 0, m.end() - 1
    while j < len(msk):
        if msk[j] == "(":
            d += 1
        elif msk[j] == ")":
            d -= 1
            if d == 0:
                break
        j += 1
    if d != 0:
        return src, False
    # ')' 之后允许 ": ReturnType", 然后必须紧跟 '{';
    # 若先撞上 '=' 就是表达式体, 不碰它。
    m2 = re.search(r'[={]', msk[j + 1:])
    if not m2 or m2.group(0) != "{":
        return src, False
    return replace_fun_body(src, sig_pattern, body)


def patch_speech_commons(root):
    """commons 侧把语音输入的实现抽空(保留签名, 见本段顶部说明)。"""
    p = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/extensions",
                  "Activity.kt")
    if not p:
        warn("找不到 commons Activity.kt, 跳过语音输入处理")
        return
    src = read(p)
    n = 0
    for sig, body in SPEECH_STUBS:
        new, ok = _stub_fun(src, sig, body)
        if ok:
            src = new
            n += 1
        else:
            warn("commons: 未能抽空 %s (签名变了或它是表达式体函数)" % sig)
    if n:
        # 判断"还有没有别处在用"时, 必须先把 import 那行本身摘掉再检查 ——
        # 否则 import 行自带的 "RecognizerIntent" 会让判断永远成立, import 永远删不掉。
        body = re.sub(r'(?m)^import android\.speech\.RecognizerIntent\n', '', src)
        if 'RecognizerIntent' not in body:
            src = body
        write(p, src)
        log("commons: 已抽空 %d/2 个语音输入函数 (speechToText / "
            "isSpeechToTextAvailable) —— 保留签名以免 app 侧 Unresolved reference"
            % n)
    else:
        warn("commons: 语音输入函数一个都没抽空, 请检查签名")


# 匹配 "if / else if (... isSpeechToTextAvailable ...)"。
# 条件里允许 ! 、&& 连接、以及带括号的调用形式 isSpeechToTextAvailable() ——
# 只写 'isSpeechToTextAvailable' 会漏掉带 () 的写法(drop_branch 的模板
# 是 `if \(COND\) \{`, 多一对括号就匹配不上)。
SPEECH_COND_AVAIL = (r'(?:[^()]|\([^()]*\))*isSpeechToTextAvailable\s*'
                     r'(?:\([^()]*\))?(?:[^()]|\([^()]*\))*')

P_DECL = r'(?m)^[ \t]*private var isSpeechToTextAvailable = false\n'
P_ASSIGN = (r'(?m)^[ \t]*isSpeechToTextAvailable = if '
            r'\(config\.useSpeechToText\).*\n')


def _report_speech_left(root):
    """清扫后复查还剩多少引用。

    commons 侧已抽空(见 SPEECH_STUBS), 所以残留不会再导致编译失败,
    只意味着"某个入口还没拿干净"(比如按钮仍绑着 speechToText)。
    报出来给人确认, 不中断构建。
    """
    rx = re.compile(r'(?<![\w])isSpeechToTextAvailable|(?<![\w])speechToText')
    left = []
    for p in walk_files(os.path.join(root, "app", "src"), (".kt",)):
        try:
            lines = _mask(read(p)).split("\n")
        except Exception:
            continue
        if any(rx.search(l) for l in lines):
            left.append(rel(root, p))
    if left:
        warn("仍有 %d 个文件引用语音输入 (commons 已抽空, 不影响编译): %s"
             % (len(left), left[:4]))
    else:
        log("语音输入: app 侧引用已全部清除")


def patch_speech_app(root):
    """app 侧彻底删掉语音输入: 设置开关 + 发送键变麦克风 + 长按 + 结果回填。

    安全设计: 只有当所有条件分支都删干净了, 才去删变量声明和 import。
    反过来(先删声明)一旦某个分支没匹配上, 就会留下 Unresolved reference,
    编译期才炸 —— 这个顺序不能颠倒。
    """
    layout = os.path.join(root, "app", "src", "main", "res", "layout",
                          "activity_settings.xml")
    if os.path.exists(layout):
        s = read(layout)
        s, ok = set_view_gone(s, "settingsUseSpeechToTextHolder")
        if ok:
            write(layout, s)
            log("activity_settings.xml: 「语音输入」设置项已隐藏")
        else:
            warn("activity_settings.xml 未找到 settingsUseSpeechToTextHolder")
    else:
        warn("找不到 activity_settings.xml")

    # --- SettingsActivity: 删设置项函数与调用 ---
    q = find_file(root, "app/src/main/kotlin/com/goodwy/smsmessenger/activities",
                  "SettingsActivity.kt")
    if q:
        s = read(q)
        s = re.sub(r'(?m)^[ \t]*setupUseSpeechToText\(\)\n', '', s)
        s, ok = drop_fun(s, r'private fun setupUseSpeechToText\(')
        write(q, s)
        log("SettingsActivity.kt: 语音输入设置项已删除%s"
            % ("" if ok else " (函数未匹配, 仅删了调用)"))
    else:
        warn("找不到 SettingsActivity.kt")

    # --- 全仓清扫: 不能只盯着 ThreadActivity ---
    # 语音输入的调用点散落在 MainActivity / NewConversationActivity /
    # ThreadActivity 好几个文件里, 写死文件名 = 漏一个就编译失败。
    # 这里改成遍历 app/src 下所有 .kt, 同一套规则逐文件处理。
    n_branch = n_result = n_decl = n_files = 0
    for p in walk_files(os.path.join(root, "app", "src"), (".kt",)):
        try:
            src = read(p)
        except Exception:
            continue
        if not any(k in src for k in ("isSpeechToTextAvailable", "speechToText",
                                      "REQUEST_CODE_SPEECH_INPUT")):
            continue
        orig = src
        src, c1 = drop_branch(src, SPEECH_COND_AVAIL)
        src, c2 = drop_branch(src, r'requestCode == REQUEST_CODE_SPEECH_INPUT')

        # 只有分支清干净了才动声明/import, 否则会留 Unresolved reference。
        # 注意: 判断残留时必须先把"声明行/赋值行本身"摘掉再检查 ——
        # 它们自身就含 isSpeechToTextAvailable, 不摘掉会永远判定为有残留,
        # 结果变量声明永远删不掉(正是上一版踩的坑)。
        body = re.sub(P_DECL, '', src)
        body = re.sub(P_ASSIGN, '', body)
        if [l for l in body.split("\n") if 'isSpeechToTextAvailable' in l]:
            body = src       # 还有引用, 声明必须留着
        else:
            n_decl += 1
        body2 = re.sub(r'(?m)^import android\.speech\.RecognizerIntent\n', '', body)
        if 'RecognizerIntent' not in body2:
            body = body2
        if body != orig:
            write(p, body)
            n_files += 1
        n_branch += c1
        n_result += c2
    log("语音输入清扫: %d 个文件被改动 (删 %d 个可用分支 + %d 个结果回填块"
        " + %d 处变量声明)" % (n_files, n_branch, n_result, n_decl))

    _report_speech_left(root)


# -------------------------------------------------- 8b. 更新日志 (What's New)
def patch_whatsnew_commons(root):
    """commons 侧废掉更新日志。

    app 侧有三个入口(启动自动弹 / 设置页右上角菜单 / checkWhatsNew),
    这里从源头掐断, 即使 app 侧有漏网的调用也只是空转。
    """
    # 1) checkWhatsNew() 置空
    p = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/extensions",
                  "Activity.kt")
    if p:
        src = read(p)
        new, ok = replace_fun_body(src, r'fun BaseSimpleActivity\.checkWhatsNew\(',
                                   '\n        return\n    ')
        if ok:
            write(p, new)
            log("commons: checkWhatsNew() 已置空 (更新日志不再自动弹出)")
        else:
            warn("commons: 未定位到 checkWhatsNew()")
    else:
        warn("找不到 commons Activity.kt, 跳过 checkWhatsNew 置空")

    # 2) WhatsNewDialog 空壳化
    p = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/dialogs",
                  "WhatsNewDialog.kt")
    if not p:
        warn("找不到 WhatsNewDialog.kt, 跳过")
        return
    src = read(p)
    if "更新日志已移除" in src:
        log("commons: WhatsNewDialog 已处理过, 跳过")
        return
    new, ok = replace_fun_body(src, r'init\s*\{',
                               '\n        // 更新日志已移除: 不弹窗\n    ')
    if ok:
        write(p, new)
        log("commons: WhatsNewDialog 已空壳化")


# ------------------------------------------------------- 8. 签名认证弹窗
def patch_sideload_dialog(root):
    """干掉"应用已损坏/请从商店重新下载"的签名认证弹窗 (AppSideloadedDialog)。

    三道保险, 缺一不可:
      1) init{} 改成直接 callback() —— 等于"用户按了取消", 流程继续往下走。
         比单纯"不 inflate 布局"安全: 有些调用方等 callback 才继续初始化,
         不回调就会卡在 Splash 黑屏。
      2) Compose 版 AppSideLoadedAlertDialog 空壳化, 不显示任何东西。
      3) BaseConfig.appSideloadingStatus 恒为 SIDELOADING_FALSE (见 patch_baseconfig),
         即使别处还有 if 判断也走不到弹窗分支。
    """
    p = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/dialogs",
                  "AppSideloadedDialog.kt")
    if not p:
        warn("找不到 AppSideloadedDialog.kt, 签名弹窗可能仍会弹出")
        return
    src = read(p)

    if "签名认证已移除" in src:
        log("commons: AppSideloadedDialog 已处理过, 跳过")
        return

    new, ok = replace_fun_body(src, r'init\s*\{',
                               '\n        // 签名认证已移除: 不弹窗, 直接放行\n'
                               '        callback()\n    ')
    if ok:
        src = new
        log("commons: AppSideloadedDialog 已空壳化 (不弹窗, 直接回调继续)")
    else:
        warn("commons: AppSideloadedDialog init{} 未匹配")

    new2, ok2 = replace_fun_body(
        src, r'fun AppSideLoadedAlertDialog\(',
        '\n    // 签名认证已移除: 不显示\n    alertDialogState.hide()\n')
    if ok2:
        src = new2
        log("commons: AppSideLoadedAlertDialog (Compose 版) 已空壳化")
    write(p, src)

    # 调用点不在本函数里处理 —— 留给收尾的 _clean_sideload_calls(),
    # 它会把 showSideloadingDialog() / checkAppSideloading() 的调用整体换成 Unit。
    # 必须走那里而不是这里: 朴素正则会连 `fun Activity.showSideloadingDialog() {`
    # 这个定义一起注释掉 -> fun Activity.// xxx { -> 语法错误, 编译期才炸。


# ------------------------------------------- "fake version" 盗版弹窗 (独立于签名认证)
# 这是跟 AppSideloadedDialog 完全无关的另一套检测, 一共三处, 文案都是
# "You are using a fake version of the app...":
#
#   1) BaseSimpleActivity.onCreate():
#        if (!packageName.startsWith("com.goodwy.", true) && !isNewApp())
#            if ((0..50).random() == 10 || appRunCount % 100 == 0) showModdedAppWarning()
#      —— 改包名后 startsWith 恒 false, 条件恒真; 随机 1/51 或启动满 100 次触发。
#      这就是为什么"之前不弹、改完包名开始弹"。
#
#   2) BaseSimpleActivity 定制页入口:
#        if (!packageName.contains("ywdoog".reversed(), true))     // == "goodwy"
#            if (appRunCount > 100) { showModdedAppWarning(); return }
#      —— 把 "goodwy" 写成反转形式躲避字符串搜索; 且带 return, 弹完还会
#         阻止进入定制页。
#
#   3) Compose 版 fakeVersionCheck(), 由 AppTheme.OnContentDisplayed() 调用,
#      几乎覆盖所有 Compose 页面。
#
# 处理策略必须"抽空实现"而不是"改判断条件": 条件是运行时对 packageName 求值,
# 而且作者用了反转字符串来防搜索, 靠正则改条件必然漏。把被调用的函数体抽空,
# 无论哪条路径走进来都是空操作。
FAKE_FUNCS = (
    (r'fun BaseSimpleActivity\.showModdedAppWarning\(',
     '\n    // 盗版检测已移除: 改包名后会误判, 不再弹窗\n'),
    (r'fun Context\.fakeVersionCheck\(',
     '\n    // 盗版检测已移除: 改包名后会误判, 不再弹窗\n'),
)


def patch_fake_version(root):
    """干掉"You are using a fake version of the app"盗版弹窗(三处入口)。

    两个函数不在一个文件里(showModdedAppWarning 在 extensions/Activity.kt,
    fakeVersionCheck 在 compose/extensions/ActivityExtensions.kt), 所以不能
    按固定路径找 —— 早先就是写死路径, 结果 Compose 那个根本没被碰到。
    改成遍历 commons 下所有 .kt, 谁含这个签名就改谁。
    """
    # 首选做法: 直接删掉判定块。删掉后运行里连 packageName 判断都不执行,
    # 无论上游再怎么改函数名/加调用点都不会弹 —— 这才是"根除"。
    dropped = _drop_fake_branches(root)
    _drop_fake_compose_call(root)

    # 兜底: 判定块万一没匹配上(上游改了条件写法), 再把被调用的函数抽空,
    # 保证"即便漏删分支也弹不出来"。两层是刻意叠加的, 不是重复劳动。
    done = 0
    for sig, body in FAKE_FUNCS:
        hit = False
        for p in walk_files(root, (".kt",)):
            try:
                src = read(p)
            except Exception:
                continue
            if "盗版检测已移除" in src and re.search(sig, _mask(src)):
                hit = True
                break
            new, ok = _stub_fun(src, sig, body)
            if ok:
                write(p, new)
                hit = True
                log("commons: 已抽空 %s (%s)"
                    % (sig.split("\\")[-1].rstrip("("), rel(root, p)))
                break
        if hit:
            done += 1
        else:
            warn("commons: 未找到/未能抽空 %s" % sig)
    if done:
        log("commons: 盗版弹窗函数已抽空 %d/%d 个" % (done, len(FAKE_FUNCS)))

    # 定制页入口那处除了弹窗还带 return, 光抽空函数不够 ——
    # return 仍会执行, 定制页永远进不去。整块删掉。
    bsa = find_file(root, "commons/src/main/kotlin/com/goodwy/commons/activities",
                    "BaseSimpleActivity.kt")
    if bsa:
        src = read(bsa)
        new, k = drop_branch(src, r'baseConfig\.appRunCount > 100')
        if k:
            write(bsa, new)
            log("commons: 定制页入口的盗版拦截已删除 (%d 处) "
                "—— 否则 appRunCount>100 后连定制页都进不去" % k)
    else:
        warn("找不到 BaseSimpleActivity.kt, 定制页盗版拦截未处理")

    # Compose 的 FakeVersionCheck() 由 AppTheme.OnContentDisplayed() 调用,
    # 几乎覆盖所有 Compose 页面。它本身只是把 fakeVersionCheck 的结果接到
    # 弹窗 state 上 —— 上面已把 fakeVersionCheck 抽空, 所以这里其实已经
    # 永远弹不出来了。但仍然空壳化它: 一是省掉每次进页面都建一次 dialog state,
    # 二是让 FAKE_VERSION_APP_LABEL 变成无人引用(下面才能安全删掉)。
    for p in walk_files(root, (".kt",)):
        try:
            src = read(p)
        except Exception:
            continue
        if "盗版检测已移除" in src and re.search(r'fun FakeVersionCheck\(', _mask(src)):
            break
        new, ok = _stub_fun(src, r'fun FakeVersionCheck\(',
                            '\n    // 盗版检测已移除: 不显示\n')
        if ok:
            write(p, new)
            log("commons: FakeVersionCheck (Compose 入口) 已空壳化 (%s)"
                % rel(root, p))
            break

    _drop_fake_version_label(root)


# 盗版检测的 if 判定块。直接删掉整块, 而不是只把被调用的函数抽空 ——
# 抽空只是"弹窗变空操作", 判断逻辑每次 onCreate 仍会跑一遍; 删块才是
# 连判断都不存在。
#
# 两处条件的写法都值得记一笔:
#   第一处直接判断 packageName.startsWith("com.goodwy."), 改包名后恒 false ->
#   取反后恒 true, 于是"改完包名才开始弹"。
#   第二处把 "goodwy" 写成 "ywdoog".reversed() —— 作者刻意反转来躲避字符串
#   搜索, 所以只搜 "goodwy" 是搜不到的。
FAKE_BRANCH_CONDS = (
    r'!packageName\.startsWith\("com\.goodwy\.", true\) && !isNewApp\(\)',
    r'!packageName\.contains\("ywdoog"\.reversed\(\), true\)',
)

# 定位 if 块头。用"找配对括号"而不是"一行正则": 条件可能跨行(上游真就写成了
# if (\n  !packageName... \n) { 这种三行形态), 一行正则直接漏掉。
IF_HEAD_RX = re.compile(r'(?m)^([ \t]*)(?:\} )?(else )?if\s*\(')

# 判定"这是盗版检测块"的两个依据, 命中任一即可:
#   1) 块内调用了 showModdedAppWarning / fakeVersionCheck
#   2) 块头条件(取原文, 不取 mask)匹配盗版判定的特征写法
#
# 依据 2 不能少: 有一处变种是"静默 finish()" —— 不弹窗, 点了选色直接关闭
# Activity, 块里一个盗版函数调用都没有, 只按依据 1 会整个漏掉。
#
# 依据 2 必须极度精确, 否则会误删功能逻辑。踩过的坑: 早先只搜 "com.goodwy."
# 这个子串, 结果把 MyContactsContentProvider 里的联系人访问白名单
#   if (packageName != "com.goodwy.dialer" && ...) return contacts
# 当成盗版检测删了 —— 那是"只允许自家 app 读联系人"的安全校验, 删掉等于
# 任何 app 都能通过 ContentProvider 拿到联系人。性质比弹窗严重得多。
#
# 区分点在于: 盗版判定用的是"前缀通配 + 忽略大小写"的否定式
#   !packageName.startsWith("com.goodwy.", true)      <- 前缀, 带 , true
#   !packageName.contains("ywdoog".reversed(), true)  <- 反转串, 带 , true
# 而功能逻辑用的是"全等比较", 带具体后缀, 不带 , true:
#   packageName != "com.goodwy.dialer"
#   !packageName.startsWith("com.goodwy.contacts")    <- 有后缀, 非通配
# 所以这里把 `, true`(ignoreCase 参数) 和确切字符串写进模式来区分。
FAKE_CALL_RX = re.compile(r'showModdedAppWarning\s*\(|fakeVersionCheck\s*\(')
FAKE_COND_LIT_RX = re.compile(
    r'!\s*packageName\s*\.\s*startsWith\s*\(\s*"com\.goodwy\."\s*,\s*true\s*\)'
    r'|!\s*packageName\s*\.\s*contains\s*\(\s*"ywdoog"\s*\.\s*reversed\s*\(\s*\)'
    r'\s*,\s*true\s*\)')


def _drop_fake_branches(root):
    """删掉盗版检测的 if 判定块(整块, 含嵌套的内层 if)。

    按"块内是否调用 showModdedAppWarning / fakeVersionCheck"来判定, 而不是
    按条件字符串匹配 —— 上游改了条件写法也能命中。
    """
    total = 0
    for p in walk_files(root, (".kt",)):
        try:
            src = read(p)
        except Exception:
            continue
        msk = _mask(src)
        spans = []
        for m in IF_HEAD_RX.finditer(msk):
            # 从 '(' 找配对的 ')'
            lp = m.end() - 1
            d, rp = 0, lp
            while rp < len(msk):
                if msk[rp] == "(":
                    d += 1
                elif msk[rp] == ")":
                    d -= 1
                    if d == 0:
                        break
                rp += 1
            if d != 0:
                continue
            # ')' 之后跳过空白与换行, 必须是 '{' 才是块形态(不是 if (x) foo())
            k = rp + 1
            while k < len(msk) and msk[k] in " \t\r\n":
                k += 1
            if k >= len(msk) or msk[k] != "{":
                continue
            depth, j = 0, k
            while j < len(msk):
                if msk[j] == "{":
                    depth += 1
                elif msk[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth != 0:
                continue
            is_fake = (FAKE_CALL_RX.search(msk[k:j + 1])
                       or FAKE_COND_LIT_RX.search(src[m.start():k + 1]))
            if not is_fake:
                continue
            # 后面若紧跟 else 就不能只删 if 那段 —— 会剩孤儿 else, 语法崩。
            # 这种情况交给"抽空函数"兜底, 效果一样(条件恒走不到弹窗)。
            rest = msk[j + 1:]
            k2 = 0
            while k2 < len(rest) and rest[k2] in " \t\r\n":
                k2 += 1
            if rest[k2:k2 + 4] == "else":
                continue
            # "} else if (...) {" 形态: 只删 else 之后那一段
            start = (m.start() + m.group(0).index("else")) if m.group(2) else m.start()
            spans.append((start, j + 1))
        if not spans:
            continue
        # 只保留最外层: 盗版检测是 if 里套 if, 内外两层的块内都能搜到
        # showModdedAppWarning, 于是都进了 spans。若两层都删, 内层先删会把
        # 外层的 end 下标整个前移, 外层再按旧下标删就多切走几个 '}' ——
        # 实测留下 '{' 比 '}' 少 3 个, 编译期才炸。所以必须丢掉被包含的。
        outer = [s for s in spans
                 if not any(o is not s and o[0] < s[0] and s[1] <= o[1]
                            for o in spans)]
        out = src
        for start, end in sorted(outer, reverse=True):
            out = out[:start] + out[end:]
        write(p, out)
        total += len(outer)
    if total:
        log("commons: 盗版检测判定块已直接删除 (%d 处) "
            "—— 连 packageName 判断都不再执行" % total)
    return total


def _drop_fake_compose_call(root):
    """删掉 AppTheme 里 FakeVersionCheck() 的调用行, 以及随之失效的 import。

    只删调用、不删定义: 定义留着无害(R8 会 shrink), 删了反而可能在别的
    模块引用时炸 unresolved reference。

    import 也要跟着删 —— 调用没了以后它是个孤立的未使用 import, 留着只是
    让"盗版检测"这几个字继续出现在源码里。
    """
    call_rx = re.compile(r'(?m)^[ \t]*FakeVersionCheck\(\)[ \t]*\n')
    imp_rx = re.compile(
        r'(?m)^import\s+[\w.]*\.?FakeVersionCheck\s*\n')
    n = 0
    for p in walk_files(root, (".kt",)):
        try:
            s = read(p)
        except Exception:
            continue
        s, k = call_rx.subn('', s)
        if not k:
            continue
        # 删掉调用后, 若本文件里 FakeVersionCheck 只剩 import 那一次出现,
        # 说明这个 import 已经没人用了, 一并删掉。
        if s.count("FakeVersionCheck") <= 1:
            s, _ = imp_rx.subn('', s)
        write(p, s)
        n += k
    return n
# 引用了。留着的话 "You are using a fake version of the app..." 这串文案会原样
# 躺在 dex 里 —— 虽然永远不会显示, 但反编译/商店扫描看得见, 属于该清的残留。
FAKE_LABEL_RX = re.compile(
    r'(?m)^const val FAKE_VERSION_APP_LABEL\s*=\s*\n?\s*"[^"]*"\n')


def _drop_fake_version_label(root):
    """删掉 FAKE_VERSION_APP_LABEL 常量 —— 前提是没有别处在引用它。"""
    for p in walk_files(root, (".kt",)):
        try:
            s = read(p)
        except Exception:
            continue
        if "FAKE_VERSION_APP_LABEL" not in s:
            continue
        if not FAKE_LABEL_RX.search(s):
            continue
        # 全仓计数: 定义 1 次 + 引用 N 次。只有恰好 1 次(即纯定义)才删,
        # 免得误删导致 Unresolved reference。
        uses = 0
        for q in walk_files(root, (".kt",)):
            try:
                uses += read(q).count("FAKE_VERSION_APP_LABEL")
            except Exception:
                pass
        if uses > 1:
            warn("FAKE_VERSION_APP_LABEL 仍被引用 %d 次, 保留以免编译失败" % uses)
            return 0
        write(p, FAKE_LABEL_RX.sub('', s))
        log("commons: 已删除 FAKE_VERSION_APP_LABEL 文案常量 (不再有人引用)")
        return 1
    return 0
NON_LANG_QUAL = {
    "night", "notnight", "land", "port", "square", "round",
    "ldpi", "mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi", "nodpi", "tvdpi", "anydpi",
    "small", "normal", "large", "xlarge", "long", "notlong",
    "watch", "television", "car", "vrheadset", "foldable", "smallestwidth",
    "sw", "w", "h", "v", "keyshidden", "keysexposed", "nokeys", "qwerty", "12key",
    "navexposed", "navhidden", "nonav", "dpad", "trackball", "wheel",
    "finger", "stylus", "touchscreen", "notouch",
}

LANG_KEEP = ("zh",)              # 只保留中文
LANG_DROP_ZH = ("tw", "hk", "mo", "hant")   # 中文里的繁体/非大陆变体, 同样删


def _is_lang_dir(name, keep=LANG_KEEP):
    if not name.startswith("values-"):
        return False
    qual = name[len("values-"):].lower()

    if qual.startswith("b+"):
        parts = [p for p in qual[2:].split("+") if p]
        if not parts:
            return False
        if parts[0] not in keep:
            return True
        return any(p in LANG_DROP_ZH for p in parts[1:])

    first = qual.split("-")[0]
    if first in NON_LANG_QUAL or first.startswith("sw") or first.startswith("v"):
        return False
    if len(first) not in (2, 3) or not first.isalpha():
        return False
    if first not in keep:
        return True
    for seg in qual.split("-")[1:]:
        if seg.startswith("r"):
            seg = seg[1:]
        if seg in LANG_DROP_ZH:
            return True
    return False


def _all_res_dirs(root):
    seen = set()
    for dp, dn, fn in os.walk(root):
        parts = dp.replace("\\", "/").split("/")
        if any(x in (".git", "build", ".gradle", ".idea") for x in parts):
            continue
        if dp.replace("\\", "/").endswith("/src/main/res") and dp not in seen:
            seen.add(dp)
            yield dp


def patch_res_languages(root):
    """删除非简体中文的 values-* 目录 (app / commons / strings 三个模块全覆盖)"""
    removed, kept = [], []
    for res in _all_res_dirs(root):
        for d in sorted(os.listdir(res)):
            full = os.path.join(res, d)
            if not os.path.isdir(full) or not d.startswith("values-"):
                continue
            if _is_lang_dir(d):
                try:
                    shutil.rmtree(full)
                    removed.append(os.path.relpath(full, root))
                except Exception as e:
                    warn("删除 %s 失败: %s" % (d, e))
            else:
                kept.append(d)
    if removed:
        log("语言精简: 已删除 %d 个语言目录, 剩余 [%s]"
            % (len(removed), ", ".join(sorted(set(kept))) or "仅 values"))
    else:
        warn("没有找到可删除的语言目录, 请确认 res 路径")
    return len(removed)


def patch_locale_filters(root, locales=("zh-rCN",)):
    """用 androidResources.localeFilters 只打包指定语言。
    删目录只管得了本项目和 commons, 第三方 AAR(androidx)自带的 values-zh / values-en
    会照常打进 APK, 让系统"应用语言"列表多出重复中文项 —— 这个开关专门治它。"""
    p = os.path.join(root, "app", "build.gradle.kts")
    if not os.path.exists(p):
        warn("找不到 app/build.gradle.kts, 跳过 localeFilters")
        return
    s = read(p)
    if "localeFilters" in s:
        log("build.gradle.kts: 已有 localeFilters, 跳过")
        return

    vals = ", ".join('"%s"' % l for l in locales)
    m = re.search(r'(\s*)androidResources\s*\{', s)
    if m:
        # 块内已有 @Suppress 就不重复加, 避免生成重复注解
        blk_end = s.find("\n" + m.group(1) + "}", m.end())
        blk = s[m.end(): blk_end if blk_end > 0 else len(s)]
        sup = "" if "Suppress" in blk else '        @Suppress("UnstableApiUsage")\n'
        line = (sup + '        localeFilters += listOf(%s)' % vals)
        insert_at = m.end()
        s = s[:insert_at] + "\n" + line + s[insert_at:]
    else:
        m2 = re.search(r'^android\s*\{', s, re.M)
        if not m2:
            warn("找不到 android { } 块, 跳过 localeFilters")
            return
        nl = s.find("\n", m2.end())
        block = ('\n    androidResources {\n'
                 '        @Suppress("UnstableApiUsage")\n'
                 '        localeFilters += listOf(%s)\n'
                 '    }' % vals)
        s = s[:nl] + block + s[nl:]
    write(p, s)
    log("build.gradle.kts: localeFilters = %s (AAR 里多余语言一并剔掉)" % ", ".join(locales))


def patch_lint(root):
    """语言目录被删后 lint 可能因缺翻译报警, 关掉阻断"""
    p = os.path.join(root, "app", "build.gradle.kts")
    if not os.path.exists(p):
        return
    s = read(p)
    m = re.search(r'(\blint\s*\{)(.*?)(\n    \})', s, re.S)
    if m:
        body = m.group(2)
        body = re.sub(r'checkReleaseBuilds\s*=\s*\w+', 'checkReleaseBuilds = false', body)
        body = re.sub(r'abortOnError\s*=\s*\w+', 'abortOnError = false', body)
        if "checkReleaseBuilds" not in body:
            body = body.rstrip() + "\n        checkReleaseBuilds = false"
        s2 = s[:m.start(2)] + body + s[m.end(2):]
        if s2 != s:
            write(p, s2)
            log("build.gradle.kts: lint 已改为不阻断")
        return
    block = ('    lint {\n        checkReleaseBuilds = false\n'
             '        abortOnError = false\n    }\n\n')
    idx = s.find("buildTypes {")
    s = s[:idx] + block + s[idx:] if idx >= 0 else s.rstrip() + "\n\n" + block
    write(p, s)
    log("build.gradle.kts: 已关闭 lint release 阻断")


# ------------------------------------------------- 5. 隐藏「更改日期和时间格式」
def patch_settings_datefmt(root):
    """日期已经全仓统一了, 这个入口留着只会让人改回去 -> 一并隐藏。
    布局置 gone + setupChangeDateTimeFormat() 里 beGone(), 双保险。"""
    layout = os.path.join(root, "app", "src", "main", "res", "layout",
                          "activity_settings.xml")
    if os.path.exists(layout):
        s = read(layout)
        new, ok = set_view_gone(s, "settings_change_date_time_format_holder")
        if ok:
            write(layout, new)
            log("activity_settings.xml: 「更改日期和时间格式」已隐藏")
        else:
            warn("activity_settings.xml 里没找到 settings_change_date_time_format_holder")
    else:
        warn("找不到 activity_settings.xml")

    p = find_file(root, "app/src/main/kotlin/com/goodwy/smsmessenger/activities",
                  "SettingsActivity.kt")
    if not p:
        warn("找不到 SettingsActivity.kt, 仅依赖布局隐藏")
        return
    src = read(p)
    if "settingsChangeDateTimeFormatHolder.beGone()" in src:
        log("SettingsActivity.kt: 日期格式入口已处理过, 跳过")
        return
    m = re.search(r'(private fun setupChangeDateTimeFormat\(\)\s*=\s*binding\.apply\s*\{\n)',
                  src)
    if m:
        src = (src[:m.end()] + "        settingsChangeDateTimeFormatHolder.beGone()\n"
               + src[m.end():])
        write(p, src)
        log("SettingsActivity.kt: setupChangeDateTimeFormat() 已插入 "
            "settingsChangeDateTimeFormatHolder.beGone()")
    else:
        warn("SettingsActivity.kt 未匹配到 setupChangeDateTimeFormat(), 仅依赖布局隐藏")


# ------------------------------------------- 10b. 三类弹窗清理 (app 侧)
def patch_app_dialogs(root):
    """去掉三类启动/设置弹窗 + 设置页右上角的更新日志入口。

    三个弹窗的触发点全在 MainActivity:
      - 更新日志(图: 版本号卡片列表): onCreate 里的 checkWhatsNewDialog()
      - 新应用推荐("重要消息和全新开始"): onResume 里的 newAppRecommendation()
      - 数据访问披露: loadMessages() 里的 ConfirmationAdvancedDialog(warning_disclosure)

    披露弹窗这里有个坑, 必须小心:
      loadMessages() 是 if(!wasReminderWarningShown){ 弹窗+权限流程 }
                      else { 同样的权限流程 }
      而 wasReminderWarningShown 又同时控制着另外两个弹窗的开关。
      所以: 把条件改成 false(走 else 分支, 不弹窗但权限流程照常),
            再在函数开头把这个标志置 true, 保证后续逻辑一致。
    """
    p = find_file(root, "app/src/main/kotlin/com/goodwy/smsmessenger/activities",
                  "MainActivity.kt")
    if not p:
        warn("找不到 MainActivity.kt, 三类弹窗清理跳过")
        return
    src = read(p)
    orig = src

    # 1) 启动时的更新日志
    src, k1 = re.subn(
        r'([ \t]*)if \(config\.wasReminderWarningShown\) checkWhatsNewDialog\(\)',
        r'\1// if (config.wasReminderWarningShown) checkWhatsNewDialog()  '
        r'// 更新日志已移除', src)

    # 2) 新应用推荐弹窗 (只在 foss 渠道显示, 正是默认构建渠道)
    src, k2 = re.subn(r'([ \t]*)newAppRecommendation\(\)',
                      r'\1// newAppRecommendation()  // 新应用推荐弹窗已移除', src)

    # 3) 数据访问披露: 条件恒假 -> 走 else 分支, 权限流程不受影响
    src, k3 = re.subn(
        r'if \(!config\.wasReminderWarningShown\) \{',
        'if (false) {  // 数据访问披露弹窗已移除', src)

    # 4) 标志置 true, 保持后续逻辑一致 (askPermissions 等依赖它)
    if k3:
        span = fun_span(src, r'private fun loadMessages\(')
        if span:
            ins = ('\n        config.wasReminderWarningShown = true  '
                   '// 披露弹窗已移除, 直接视为已确认')
            src = src[:span[0] + 1] + ins + src[span[0] + 1:]
        else:
            warn("MainActivity.kt 未定位到 loadMessages(), 标志未设置")

    if src != orig:
        write(p, src)
    for k, msg in ((k1, "更新日志(启动时)"), (k2, "新应用推荐"),
                   (k3, "数据访问披露")):
        log("MainActivity.kt: %s %s" % (msg, "已移除" if k else "未匹配"))

    # 5) 函数体兜底置空 (万一别处调用)
    q = find_file(root, "app/src/main/kotlin/com/goodwy/smsmessenger/extensions",
                  "Activity.kt")
    if q:
        s2 = read(q)
        new2, ok2 = replace_fun_body(s2, r'fun Activity\.newAppRecommendation\(\)',
                                     '\n        return\n    ')
        if ok2:
            write(q, new2)
            log("extensions/Activity.kt: newAppRecommendation() 已置空")

    # 6) 设置页右上角工具栏的「更新日志」入口
    q = find_file(root, "app/src/main/kotlin/com/goodwy/smsmessenger/activities",
                  "SettingsActivity.kt")
    if q:
        s3 = read(q)
        new3, k4 = re.subn(r'[ \t]*R\.id\.whats_new\s*->\s*\{.*?\n[ \t]*\}\n',
                           '', s3, flags=re.S)
        if k4:
            write(q, new3)
            log("SettingsActivity.kt: 已删除工具栏「更新日志」菜单分支")
        else:
            warn("SettingsActivity.kt 未匹配到 whats_new 分支")
    else:
        warn("找不到 SettingsActivity.kt, 跳过菜单分支清理")

    # 7) 菜单项本身: 保留 id(防止 R 符号丢失), 改为不可见
    mp = os.path.join(root, "app", "src", "main", "res", "menu",
                      "menu_settings.xml")
    if os.path.exists(mp):
        s = read(mp)
        new = re.sub(r'(<item\b(?:(?!>).)*?@\+id/whats_new(?:(?!>).)*?)(/?)>',
                     lambda m: (m.group(1) if 'android:visible' in m.group(1)
                                else m.group(1).rstrip() + ' android:visible="false"')
                     + m.group(2) + '>',
                     s, flags=re.S)
        if new != s:
            write(mp, new)
            log("menu_settings.xml: 工具栏「更新日志」已设为不可见")
        else:
            warn("menu_settings.xml 未匹配到 whats_new 项")
    else:
        warn("找不到 menu_settings.xml")


def patch_other_group(root):
    """隐藏设置页的「其他」分组标题。

    这个分组里原本只有两项: 小费罐(已 gone) 和 关于(已 gone)。
    两项都隐藏后, 空分组标题 + 空卡片会留着一个突兀的「其他」字样,
    所以把标题和容器一起收掉。
    """
    layout = os.path.join(root, "app", "src", "main", "res", "layout",
                          "activity_settings.xml")
    if not os.path.exists(layout):
        warn("找不到 activity_settings.xml, 跳过「其他」分组清理")
        return
    s = read(layout)
    hit = []
    for vid in ("settingsOtherLabel", "settingsOtherHolder"):
        s, ok = set_view_gone(s, vid)
        if ok:
            hit.append(vid)
    if hit:
        write(layout, s)
        log("activity_settings.xml: 「其他」分组已隐藏 (%s)" % ", ".join(hit))
    else:
        warn("activity_settings.xml 未找到「其他」分组 id")


# ------------------------------------------------- 9. 购买 Thank You / 内购
def patch_purchase_card(root):
    """删掉设置页里的「购买 Thank You」卡片和「小费罐 / Tip Jar」。

    注意 Goodwy 的 PurchaseThankYouItem.updateVisibility() 和 Fossify 不同:
    它只设置颜色和 drawable, 里面没有 beGoneIf —— 也就是说光在 XML 里写
    visibility="gone" 是够的(不会被 Kotlin 改回来)。但设置页代码里有
    `settingsPurchaseThankYouHolder.beGoneIf(isPro())`, 未购买时 isPro()==false
    -> 实际会 beVisible(), 把布局的 gone 又覆盖掉。所以必须改 Kotlin。
    """
    layout = os.path.join(root, "app", "src", "main", "res", "layout",
                          "activity_settings.xml")
    if os.path.exists(layout):
        s = read(layout)
        for vid in ("settingsPurchaseThankYouHolder", "settingsTipJarHolder"):
            s, ok = set_view_gone(s, vid)
            if ok:
                log("activity_settings.xml: 已隐藏 %s" % vid)
            else:
                warn("activity_settings.xml 里没找到 %s" % vid)
        write(layout, s)
    else:
        warn("找不到 activity_settings.xml")

    p = find_file(root, "app/src/main/kotlin/com/goodwy/smsmessenger/activities",
                  "SettingsActivity.kt")
    if not p:
        warn("找不到 SettingsActivity.kt, 仅依赖布局隐藏")
        return
    src = read(p)

    # beGoneIf(isPro()) -> beGone(): 不购买时 isPro()==false -> 实际 beVisible(),
    # 会把布局里写的 gone 又覆盖掉, 所以必须无条件隐藏。
    # 注意: 参数里带嵌套括号(isPro()), 正则要配平一层,
    # 用 [^)]* 会在第一个 ')' 截断, 留下多余的 ')' 导致语法错误。
    BAL = r'\((?:[^()]|\([^()]*\))*\)'
    src, k1 = re.subn(r'settingsPurchaseThankYouHolder\.beGoneIf' + BAL,
                      'settingsPurchaseThankYouHolder.beGone()', src)
    if k1:
        log("SettingsActivity.kt: 购买卡片改为无条件隐藏")

    # setupPurchaseThankYou / setupTipJar: 函数体开头插 beGone()
    for fn_name, holder in (("setupPurchaseThankYou",
                             "settingsPurchaseThankYouHolder"),
                            ("setupTipJar", "settingsTipJarHolder")):
        if "%s.beGone()" % holder in src:
            continue
        m = re.search(r'(private fun %s\(\)\s*=\s*binding\.apply\s*\{\n)' % fn_name, src)
        if m:
            src = src[:m.end()] + "        %s.beGone()\n" % holder + src[m.end():]
            log("SettingsActivity.kt: %s() 已插入 %s.beGone()" % (fn_name, holder))
        else:
            warn("SettingsActivity.kt 未匹配到 %s()" % fn_name)

    # setupTipJar() 里 apply{} 块内是隐式 this, 那句 beVisibleIf 也必须改,
    # 否则它会在我们插入的 beGone() 之后又把卡片显示回来。
    src, k2 = re.subn(r'(\n\s*)beVisibleIf\(isPro\(\)\)', r'\1beGone()', src)
    if k2:
        log("SettingsActivity.kt: 已把 %d 处 beVisibleIf(isPro()) 改为 beGone()" % k2)

    # updatePro() 里也会复活它们
    src = src.replace("settingsTipJarHolder.beVisibleIf(isPro)",
                      "settingsTipJarHolder.beGone()")
    src = re.sub(r'settingsPurchaseThankYouHolder\.beGoneIf' + BAL,
                 'settingsPurchaseThankYouHolder.beGone()', src)
    write(p, src)

    # 语法自检: 残留 ".beGone())" 说明括号被截断了
    bad = [l.strip() for l in src.split("\n") if ".beGone())" in l]
    if bad:
        warn("SettingsActivity.kt 疑似括号不匹配: %s" % bad[:2])


def patch_google_trim(root):
    """精简 Google 相关: 移除 play-services 依赖, 打开 hide_google_relations 开关。

    只做"确定安全"的两件事:
      - play-services-location(google-services) 是个几百 KB 的大依赖, 删掉。
      - hide_google_relations = true: commons 官方的总开关。
    gplay flavor 的 billing 不动 —— 它只在 gplay 渠道编译, 默认构建的是 foss。
    """
    # 1) 依赖
    hit = []
    for p in walk_files(root, (".toml", ".kts")):
        try:
            s = read(p)
        except Exception:
            continue
        o = s
        s = re.sub(r'\n[ \t]*(?:implementation|api)\((?:libs\.)?google[.-]services\)',
                   '', s)
        s = re.sub(r'\n[ \t]*google-services\s*=\s*\{[^}]*\}', '', s)
        if s != o:
            write(p, s)
            hit.append(rel(root, p))
    if hit:
        log("Google 精简: 已移除 play-services 依赖 (%s)" % ", ".join(hit))
    else:
        log("Google 精简: 未找到 play-services 依赖条目")

    # 2) 官方总开关
    pat = re.compile(r'(<bool\s+name="hide_google_relations"\s*>)\s*(?:true|false)\s*(</bool>)')
    n = 0
    for res in _all_res_dirs(root):
        for d in sorted(os.listdir(res)):
            if not d.startswith("values"):
                continue
            full = os.path.join(res, d)
            if not os.path.isdir(full):
                continue
            for f in sorted(os.listdir(full)):
                if not f.endswith(".xml"):
                    continue
                fp = os.path.join(full, f)
                try:
                    s = read(fp)
                except Exception:
                    continue
                new, k = pat.subn(r'\1true\2', s)
                if k:
                    write(fp, new)
                    n += k
    if n:
        log("Google 精简: hide_google_relations = true (%d 处)" % n)
    else:
        log("Google 精简: 未找到 hide_google_relations 开关 (commons 里可能已硬编码)")


# ------------------------------------------------------------------ 10. 改包名
DEFAULT_OLD_PKG = "com.goodwy.smsmessenger"
DEFAULT_NEW_PKG = "com.android.messages"

# 这些前缀是保留命名空间, 用了会有麻烦, 但脚本只警告不阻止 ——
# 自用/开源深度定制场景下用户可能就是想要, 拦下来属于越权。
#
# 为什么 com.android.* 有风险, 说清楚:
#   1) Google Play 官方明确禁止: "Both the com.example and com.android
#      namespaces are forbidden by Google Play" —— 上商店 100% 被拒。
#   2) 部分 ROM 的包安装器会拒绝安装 com.android.* 开头的第三方 APK。
#      有实测案例: 包名用 com.android.simple 时 adb install 正常, 但点 APK
#      安装报"应用未安装", 改掉包名后恢复。所以装不上时先换 adb install 试。
#   3) com.example 同理被 Play 禁止。
# 注意: AOSP 原生短信应用的包名是 com.android.messaging (单数, 没有 s),
# 和这里的 com.android.messages (复数) 不冲突, 不会装不上。
RESERVED_PKG_PREFIXES = (
    ("com.android.", "Google Play 明令禁止; 部分 ROM 拒绝安装, 且易与系统应用混淆"),
    ("com.example.", "Google Play 明令禁止的示例命名空间"),
    ("com.google.", "属于 Google 的命名空间, 非官方应用不得使用"),
    ("android.", "Android 平台 API 命名空间, 第三方不可用"),
    ("java.", "Java 平台命名空间, 会导致类加载冲突"),
)


def _warn_reserved_pkg(pkg):
    for prefix, why in RESERVED_PKG_PREFIXES:
        if pkg.startswith(prefix):
            warn("包名 %r 以保留前缀 %r 开头: %s" % (pkg, prefix, why))
            warn("  自用/开源定制可以继续, 但请注意: 上传到 Google Play 会被拒; "
                 "若设备安装时报'应用未安装', 改用 adb install 再试")
            return True
    return False


def patch_package(root, new_pkg, old_pkg=DEFAULT_OLD_PKG):
    """把 app 包名从 com.goodwy.smsmessenger 改成自定义包名。

    关键点:
      - namespace / applicationId 都读 gradle.properties 的 APP_ID, 改这一个值即可;
        但源码里的 `package com.goodwy.smsmessenger` 必须同步改, 否则 R / BuildConfig 引用断裂。
      - 只替换 com.goodwy.smsmessenger, 绝不碰 com.goodwy.commons (那是库的包名)。
      - Manifest 里 <queries> 列出的是"其它 Goodwy 应用"的包名(phone/contacts/dialer),
        它们跟新包名无关, 保持原样不动。
    """
    if not new_pkg or new_pkg == old_pkg:
        log("包名未变更 (%s)" % old_pkg)
        return
    if not re.match(r'^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$', new_pkg):
        warn("包名 %r 格式不合法, 跳过改名" % new_pkg)
        return

    _warn_reserved_pkg(new_pkg)

    log("改名: %s -> %s" % (old_pkg, new_pkg))
    n_files = 0
    n_hits = 0

    # 1) gradle.properties: APP_ID
    gp = os.path.join(root, "gradle.properties")
    if os.path.exists(gp):
        s = read(gp)
        new, k = re.subn(r'(?m)^(APP_ID\s*=\s*).*$', r'\1%s' % new_pkg, s)
        if k:
            write(gp, new)
            log("  gradle.properties: APP_ID = %s" % new_pkg)
        else:
            warn("  gradle.properties 里没有 APP_ID, 请手动加 APP_ID=%s" % new_pkg)
    else:
        warn("  找不到 gradle.properties")

    # 2) 源码: package / import / 全限定引用
    for p in walk_files(root, (".kt", ".java", ".xml", ".kts", ".pro")):
        try:
            s = read(p)
        except Exception:
            continue
        if old_pkg not in s:
            continue
        new, k = re.subn(re.escape(old_pkg), new_pkg, s)
        if k:
            write(p, new)
            n_files += 1
            n_hits += k
    log("  源码/资源: %d 个文件, %d 处替换" % (n_files, n_hits))

    # 3) 目录名: kotlin/com/goodwy/smsmessenger -> 新路径
    old_dir = os.path.join(root, "app", "src", "main", "kotlin",
                           *old_pkg.split("."))
    if os.path.isdir(old_dir):
        new_dir = os.path.join(root, "app", "src", "main", "kotlin",
                               *new_pkg.split("."))
        if old_dir != new_dir:
            os.makedirs(os.path.dirname(new_dir), exist_ok=True)
            if os.path.exists(new_dir):
                warn("  目标目录已存在, 跳过移动: %s" % new_dir)
            else:
                shutil.move(old_dir, new_dir)
                log("  目录已迁移: %s" % os.path.relpath(new_dir, root))
    else:
        warn("  未找到源码目录 %s (可能路径不同, 但文件内包名已改)" %
             os.path.relpath(old_dir, root) if os.path.isdir(root) else "")

    # 4) debug 变体目录(如果存在)
    dbg_old = os.path.join(root, "app", "src", "debug")
    if os.path.isdir(dbg_old):
        for p in walk_files(dbg_old, (".kt", ".xml")):
            try:
                s = read(p)
            except Exception:
                continue
            if old_pkg in s:
                write(p, s.replace(old_pkg, new_pkg))


# ------------------------------------------------------------------ 6. ABI 精简
ARCH_KEEP = "arm64-v8a"
ARCH_DROP = {"armeabi", "armeabi-v7a", "x86", "x86_64", "mips", "mips64", "riscv64"}


def patch_abi(root, arch=ARCH_KEEP):
    """只保留 64 位 ARM: 构建配置 abiFilters + 删掉源码里其它架构的 .so"""
    p = os.path.join(root, "app", "build.gradle.kts")
    if not os.path.exists(p):
        warn("找不到 app/build.gradle.kts, 跳过 ABI 精简")
        return
    s = read(p)
    if "abiFilters" in s:
        log("build.gradle.kts: 已有 abiFilters, 跳过")
    else:
        span = fun_span(s, r'defaultConfig\s*\{')
        if not span:
            warn("defaultConfig 块未找到, 跳过 ABI 精简")
        else:
            j = span[1]                      # defaultConfig 的配对 '}' 下标
            # s[:j] 末尾已含 defaultConfig 的 4 格缩进, ins 开头要再补 4 格才与 ksp 同级
            ins = ('    ndk {\n            abiFilters += "%s"\n        }\n    ' % arch)
            s = s[:j] + ins + s[j:]
            write(p, s)
            log("build.gradle.kts: ABI 只保留 %s (其余架构的 .so 不会打进 APK)" % arch)
            s = read(p)

    # 源码里的 jniLibs / libs
    removed = 0
    app_dir = os.path.join(root, "app")
    if os.path.isdir(app_dir):
        for dp, dn, fn in os.walk(app_dir):
            for d in list(dn):
                if d in ARCH_DROP:
                    try:
                        shutil.rmtree(os.path.join(dp, d))
                        removed += 1
                        dn.remove(d)
                    except Exception:
                        pass
    log("源码 jniLibs: %s" % ("已删除 %d 个非 %s 架构目录" % (removed, arch))
        if removed else "没有其它架构的 .so 目录 (ABI 过滤已由构建配置接管)")


# ------------------------------------------------------------------ 7. 设备资源精简
DEVICE_QUALS = ("sw600dp", "sw720dp", "sw480dp", "sw320dp",
                "large", "xlarge", "television", "watch", "car",
                "vrheadset", "desktop", "chromebook", "uimode")
LOW_DENSITIES = ("ldpi", "mdpi", "tvdpi", "hdpi", "xhdpi")
SAFE_DENSITIES = ("xxhdpi", "xxxhdpi")


def _qual_tail(dirname):
    """mipmap-hdpi -> 'hdpi'; values-sw600dp -> 'sw600dp'; drawable -> ''"""
    return dirname.split("-", 1)[1] if "-" in dirname else ""


def patch_device_res(root):
    """删掉只服务平板/电视/车机的资源目录, 以及能由高密度缩放得到的低密度位图。
    注意 values-land(手机横屏) / values-w600dp(折叠屏) 不在删除名单里, 不能误伤。"""
    dropped_dev, dropped_density, n_files = [], [], 0

    for res in _all_res_dirs(root):
        for d in sorted(os.listdir(res)):
            full = os.path.join(res, d)
            if not os.path.isdir(full):
                continue
            qual = _qual_tail(d)
            if not qual:
                continue
            if any(q in qual.split("-") for q in DEVICE_QUALS):
                try:
                    shutil.rmtree(full)
                    dropped_dev.append(d)
                except Exception as e:
                    warn("删除 %s 失败: %s" % (d, e))

    # 低密度位图: 只删"高密度目录里有同名文件"的, 防止资源丢失导致 aapt2 编译失败
    for res in _all_res_dirs(root):
        high = {}
        for d in SAFE_DENSITIES:
            hp = os.path.join(res, "drawable-" + d)
            if os.path.isdir(hp):
                high[d] = set(os.listdir(hp))
        if not high:
            continue
        for d in LOW_DENSITIES:
            lp = os.path.join(res, "drawable-" + d)
            if not os.path.isdir(lp):
                continue
            files = os.listdir(lp)
            if not files:
                continue
            if not all(any(f in s for s in high.values()) for f in files):
                warn("drawable-%s 里有高密度目录找不到的文件, 保守保留" % d)
                continue
            try:
                shutil.rmtree(lp)
                dropped_density.append("drawable-" + d)
                n_files += len(files)
            except Exception:
                pass

    log("设备精简: %s" % ("已删除 %d 个非手机资源目录 (%s)"
                          % (len(dropped_dev), ", ".join(sorted(set(dropped_dev)))))
        if dropped_dev else "没有平板/电视/车机专用目录")
    log("密度精简: %s" % ("已删除 %s (共 %d 个文件, 手机用 xxhdpi/xxxhdpi 缩放即可)"
                          % (", ".join(sorted(set(dropped_density))), n_files))
        if dropped_density else "未发现可安全删除的低密度位图目录")


# ------------------------------------------------------------------ 4. 斜体 -> 正体
def _fix_style_value(v):
    parts = [x.strip() for x in re.split(r'[|, ]', v) if x.strip()]
    parts = [p for p in parts if p.lower() != "italic"]
    if not parts:
        return "normal"
    return "|".join(parts)


def patch_italic(root):
    """把应用内所有斜体改回默认正体。四种写法, 缺一就是"改了但没改干净":

      XML 属性: android:textStyle="italic" / "bold|italic"
      style item: <item name="android:textStyle">italic</item>
      传统 View: Typeface.ITALIC / Typeface.BOLD_ITALIC
      Compose  : TextStyle(fontStyle = FontStyle.Italic)   <- 上一版漏的就是这个

    Compose 那条最容易漏: 它既不是 XML 属性也不含 "textStyle" 字样,
    只在 ManageBlockedNumbersScreen 这类纯 Compose 页面里出现,
    而那些页面恰恰是设置里最显眼的空列表提示文案。
    """
    n_xml = n_item = n_kt = n_compose = 0

    for p in walk_files(root, (".xml",)):
        try:
            s = read(p)
        except Exception:
            continue
        orig = s
        # 属性形式
        s, k1 = re.subn(r'((?:android:)?textStyle\s*=\s*")([^"]*)(")',
                        lambda m: m.group(1) + _fix_style_value(m.group(2)) + m.group(3), s)
        # <item name="android:textStyle">italic</item> 形式
        s, k2 = re.subn(r'(<item[^>]*name="(?:android:)?textStyle"[^>]*>)([^<]*)(</item>)',
                        lambda m: m.group(1) + _fix_style_value(m.group(2)) + m.group(3), s)
        if s != orig:
            write(p, s)
            n_xml += k1
            n_item += k2

    for p in walk_files(root, (".kt", ".java")):
        try:
            s = read(p)
        except Exception:
            continue
        orig = s
        s = s.replace("Typeface.BOLD_ITALIC", "Typeface.BOLD")
        s, k = re.subn(r'\bTypeface\.ITALIC\b', 'Typeface.NORMAL', s)
        n_kt += k
        # Compose: 换成 Normal 而不是删掉 fontStyle= —— FontStyle 的 import
        # 还得留着(Normal 仍要用), 而且 TextStyle(...) 少一个具名参数不会报错,
        # 但显式写 Normal 更清楚, 也避免将来有人再加回 Italic。
        s, kc = re.subn(r'\bFontStyle\.Italic\b', 'FontStyle.Normal', s)
        n_compose += kc
        if s != orig:
            write(p, s)

    log("斜体 -> 正体: XML 属性 %d 处, style item %d 处, "
        "Kotlin Typeface %d 处, Compose FontStyle %d 处"
        % (n_xml, n_item, n_kt, n_compose))
    if n_xml + n_item + n_kt + n_compose == 0:
        warn("一处斜体都没匹配到 —— 如果该版本确实有斜体, 请检查是否被写成自定义 font 或 span")

    _fold_dead_branches(root)


# 只吃"同一行内、两分支字面量完全相同"的 if —— 形如
#   if (conversation.isScheduled) Typeface.BOLD else Typeface.BOLD
# 这是斜体改造留下的: 原本靠 BOLD_ITALIC 区分定时短信, 斜体改成 BOLD 后
# 两个分支就相等了。恒等于 X, 折叠掉纯属等价化简。
# 不碰跨行、不碰 else if 链、不碰分支里带调用/花括号的, 避免误判语义。
# 两个分支就相等了。恒等于 X, 折叠掉纯属等价化简。
# 不碰跨行、不碰 else if 链、不碰分支里带调用/花括号的, 避免误判语义。
#
# 末尾的 (?![A-Za-z0-9_.]) 是必须的, 之前漏了它: 原正则用 \1\b 收尾, 而
# `if (noContrastColor) baseColor else baseColor.getContrastColor()` 里
# \1 匹配到第二个 baseColor 后, 紧跟的 '.getContrastColor()' 恰好让 \b 成立,
# 于是被误判成"两分支相同", 折叠后丢掉 .getContrastColor(), 颜色值静默变错。
# 这类 bug 不报编译错、只改运行时行为, 最难查, 所以负向前瞻不能省。
DEAD_BRANCH_RX = re.compile(
    r'\bif\s*\((?:[^()]|\([^()]*\))*\)\s*([A-Za-z_][A-Za-z0-9_.]*)\s+else\s+'
    r'\1(?![A-Za-z0-9_.])')


def _fold_dead_branches(root):
    n = 0
    for p in walk_files(root, (".kt", ".java")):
        try:
            s = read(p)
        except Exception:
            continue
        new, k = DEAD_BRANCH_RX.subn(lambda m: m.group(1), s)
        if k:
            write(p, new)
            n += k
    if n:
        log("死分支折叠: %d 处 if/else 两分支相同 -> 直接取该值" % n)
    return n


# ------------------------------------------------------------------ 5. 依赖 / 构建环境
def patch_commons_version(root, version):
    """commons 根 build.gradle.kts: 给所有子项目(含 strings)统一 group/version,
    否则 commons 的 POM 里会写出 com.goodwy.strings:strings:unspecified, app 侧解析不到。"""
    p = os.path.join(root, "build.gradle.kts")
    if not os.path.exists(p):
        warn("commons 根 build.gradle.kts 不存在")
        return
    s = read(p)
    if "allprojects" in s:
        log("commons 根 build.gradle.kts: 已有 allprojects, 跳过")
        return
    s = s.rstrip() + (
        '\n\nallprojects {\n'
        '    group = "com.github.goodwy.goodwy-commons"\n'
        '    version = findProperty("VERSION")?.toString()\n'
        '        ?: System.getenv("VERSION") ?: "%s"\n'
        '}\n' % version)
    write(p, s)
    log("commons 根 build.gradle.kts: 已为 allprojects 统一 group/version = %s" % version)


def patch_app_version(root, version):
    """app: 把 commons 依赖版本从 jitpack 的 commit hash 换成本地 maven 版本号"""
    changed = []
    for p in walk_files(root, (".toml", ".kts", ".gradle")):
        try:
            s = read(p)
        except Exception:
            continue
        orig = s
        s = re.sub(r'(^\s*right-commons\s*=\s*)"[^"]*"', r'\1"%s"' % version, s, flags=re.M)
        s = re.sub(r'"com\.github\.goodwy\.goodwy-commons:commons-[^"]*:[^"]*"',
                   lambda m: ':'.join(m.group(0).split(':')[:2] + ['%s"' % version]), s)
        if s != orig:
            write(p, s)
            changed.append(rel(root, p))
    if changed:
        log("commons 依赖版本 -> %s (%s)" % (version, ", ".join(changed)))
    else:
        warn("未能替换 commons 依赖版本, 请手动把 gradle/libs.versions.toml 里的 "
             "right-commons 改成 %s" % version)


LOCAL_PROPS = """sdk.dir={sdk}
RIGHT_APP_KEY=685530047
PRODUCT_ID_X1=product_x1
PRODUCT_ID_X2=product_x2
PRODUCT_ID_X3=product_x3
SUBSCRIPTION_ID_X1=sub_x1
SUBSCRIPTION_ID_X2=sub_x2
SUBSCRIPTION_ID_X3=sub_x3
SUBSCRIPTION_YEAR_ID_X1=sub_year_x1
SUBSCRIPTION_YEAR_ID_X2=sub_year_x2
SUBSCRIPTION_YEAR_ID_X3=sub_year_x3
"""


def patch_local_properties(root, sdk=None):
    """app/build.gradle.kts 会 load(rootProject.file("local.properties")),
    这个文件被 .gitignore 了, CI 里不生成会直接构建失败。"""
    p = os.path.join(root, "local.properties")
    if os.path.exists(p):
        log("local.properties 已存在, 跳过")
        return
    write(p, LOCAL_PROPS.format(
        sdk=sdk or os.environ.get("ANDROID_HOME") or "/usr/local/lib/android/sdk"))
    log("local.properties: 已生成 (含 sdk.dir 与内购占位 key)")


def set_view_gone(xml_text, view_id):
    """给布局里指定 id 的 view 加 android:visibility="gone" (保留 id, 不破坏 binding)"""
    pat = re.compile(
        r'(<[A-Za-z][\w.]*)((?:(?!>).)*?android:id="@\+id/%s")((?:(?!/>).)*?)(/?)>'
        % re.escape(view_id), re.S)
    m = pat.search(xml_text)
    if not m:
        return xml_text, False
    attrs = m.group(3)
    if "android:visibility" in attrs:
        attrs = re.sub(r'android:visibility="[^"]*"', 'android:visibility="gone"', attrs)
    else:
        attrs = attrs.rstrip() + ' android:visibility="gone"'
    return (xml_text[:m.start(3)] + attrs + m.group(4) + ">" + xml_text[m.end():], True)


# ------------------------------------------------------- 12. 收尾清理 (两侧都跑)
# 为什么必须放最后, 而且 commons / app 两侧各跑一次:
#   A) 签名/侧载认证: 前面的 patch 只把 AppSideloadedDialog 空壳化了,
#      "调用点"还原样留在源码里。改包名之后更是处处是雷 —— 自签名 +
#      非商店安装来源, 任何一处 checkAppSideloading() 被走到, 都可能把
#      「应用已损坏, 请从商店重新下载」再拉回来。把调用点换成 Unit 才叫断根。
#   B) 收费/内购痕迹: 卡片设成 gone 只是"看不见", BILLING 权限和付费文案
#      照样原封不动躺在 APK 里。开源商店(F-Droid / IzzyOnDroid)的收录扫描
#      认的就是这两个 —— manifest 里的 com.android.vending.BILLING, 和
#      resources.arsc 里的 "Purchase Thank You" / "Tip Jar" / "Donate"。
SIDELOAD_FUNCS = ("showSideloadingDialog", "checkAppSideloading")

# 允许带接收者: showSideloadingDialog() / requireActivity().showSideloadingDialog()
SIDELOAD_CALL_RX = re.compile(
    r'(?<![\w$.])(?:(?:[A-Za-z_][A-Za-z0-9_]*)'
    r'(?:\s*\((?:[^()]|\([^()]*\))*\))?\s*\.\s*)?'
    r'(?:' + '|'.join(SIDELOAD_FUNCS) + r')\s*\((?:[^()]|\([^()]*\))*\)')

PAY_NAME_KEYS = ("purchase", "thank_you", "thankyou", "tip_jar", "tipjar",
                 "donate", "donation", "subscrib", "billing", "in_app",
                 "patreon", "paypal", "support_us", "contribute",
                 "become_", "pro_version", "unlock_pro")


# 只吃"整行就是一个调用"的独立语句, 前面最多带 if (...) / else 守卫。
# 为什么这么保守: `foo(showSideloadingDialog())` 换成 foo(Unit) 会类型不匹配,
# 直接编不过; `return showSideloadingDialog()` 同理。宁可漏, 不可错 ——
# 漏掉的调用点无害: BaseConfig 恒 FALSE + 弹窗 init 直接 callback +
# Compose 版空壳, 三道保险已把它们变成空操作。
SIDELOAD_STMT_RX = re.compile(
    r'[ \t]*(?:(?:if\s*\((?:[^()]|\([^()]*\))*\)|else)[ \t]*)?'
    r'(?P<call>(?:[A-Za-z_][A-Za-z0-9_]*\s*\((?:[^()]|\([^()]*\))*\)\s*\.\s*)?'
    r'(?:' + '|'.join(SIDELOAD_FUNCS) + r')\s*\((?:[^()]|\([^()]*\))*\))'
    r'[ \t]*;?[ \t]*$')


def _sideload_calls_left(root):
    """统计剩下的调用点(排除 fun 定义行), 用于报告漏网。"""
    n = 0
    for p in walk_files(root, (".kt", ".java")):
        try:
            mlines = _mask(read(p)).split("\n")
        except Exception:
            continue
        n += sum(1 for ml in mlines
                 if SIDELOAD_CALL_RX.search(ml) and not re.search(r'\bfun\b', ml))
    return n


def _clean_sideload_calls(root):
    """把签名/侧载认证的独立语句调用整体替换成 Unit。

    逐行匹配而不是全文 finditer: _mask 只把非换行字符换成空格, 行结构与
    原文严格对齐, 所以拿 mask 行定位、回原文行切片替换是安全的 —— 注释和
    字符串里的同名调用天然被 mask 掉, 函数定义行则因为尾部还有 " {" 而
    匹配不上 (上一版就是注释定义行写坏成 `fun Activity.// ... {`, 编译期才炸)。
    """
    before = _sideload_calls_left(root)
    done, files = 0, 0
    for p in walk_files(root, (".kt", ".java")):
        try:
            src = read(p)
        except Exception:
            continue
        lines = src.split("\n")
        mlines = _mask(src).split("\n")
        hit = False
        for i, ml in enumerate(mlines):
            m = SIDELOAD_STMT_RX.match(ml)
            if not m:
                continue
            s, e = m.start("call"), m.end("call")
            lines[i] = lines[i][:s] + "Unit  // 签名认证已移除" + lines[i][e:]
            hit = True
            done += 1
        if hit:
            write(p, "\n".join(lines))
            files += 1
    if done:
        log("收尾: 签名/侧载认证调用点已置空 %d 处 (%d 个文件)" % (done, files))
    else:
        log("收尾: 未发现独立的签名/侧载认证调用点")
    left = _sideload_calls_left(root)
    if left:
        warn("收尾: 仍有 %d 处调用点嵌在表达式里(参数/返回值), 未改动以免类型不匹配"
             % left)
    log("收尾: 调用点 %d -> 剩余 %d" % (before, left))
    return done


def _clean_billing_manifest(root):
    """删掉 com.android.vending.BILLING 权限 —— 商店判定「含内购」的第一证据。"""
    pat = re.compile(
        r'[ \t]*<uses-permission\b[^>]*?com\.android\.vending\.BILLING[^>]*?/?>[ \t]*\n?',
        re.I)
    n = 0
    for p in walk_files(root, (".xml",)):
        if os.path.basename(p) != "AndroidManifest.xml":
            continue
        try:
            s = read(p)
        except Exception:
            continue
        new, k = pat.subn('', s)
        if k:
            write(p, new)
            n += k
            log("收尾: %s 已删除 BILLING 权限 (%d 处)" % (rel(root, p), k))
    if not n:
        log("收尾: 未发现 com.android.vending.BILLING 权限 (foss 渠道本就没有)")
    return n


def _clean_payment_strings(root):
    """把内购相关字符串的"内容"清空 (保留 name, 免得 R.string 引用全断)。

    清空内容而不是删条目, 是刻意的: name 还在, 任何 getString(R.string.xxx)
    都照常编译通过, 只是拿到空串 —— 而那些卡片入口本来就已经被 gone 掉了。
    按 name 匹配, 所以 values-zh-rCN / values-de 等各语种会一起被清干净。
    """
    pat = re.compile(r'(<string\s+name="([^"]+)"[^>]*>)(.*?)(</string>)', re.S)
    total, cleared = 0, []
    for p in walk_files(root, (".xml",)):
        norm = p.replace("\\", "/")
        if "/res/" not in norm:
            continue
        if not os.path.basename(os.path.dirname(norm)).startswith("values"):
            continue
        try:
            s = read(p)
        except Exception:
            continue
        seen = []

        def repl(m):
            name = m.group(2).lower()
            if any(k in name for k in PAY_NAME_KEYS) and m.group(3).strip():
                seen.append(m.group(2))
                return m.group(1) + m.group(4)
            return m.group(0)

        new = pat.sub(repl, s)
        if seen:
            write(p, new)
            total += len(seen)
            cleared.extend(seen)
    if total:
        log("收尾: 已清空 %d 条内购文案, 例: %s"
            % (total, ", ".join(sorted(set(cleared))[:4])))
    else:
        log("收尾: 未发现内购文案")
    return total


def _report_pkg_leftover(root, new_pkg, old_pkg=DEFAULT_OLD_PKG):
    """改包名后复查: 全仓还有没有旧包名的漏网之鱼。

    patch_package 只改 .kt/.java/.xml/.kts/.pro, 而 .properties/.gradle/
    .json 里也可能埋着旧包名(比如 manifestPlaceholders、deep link scheme),
    这些地方漏改 = 运行时 ClassNotFound 或 authority 冲突, 且编译期不报错。
    """
    if not new_pkg or new_pkg == old_pkg:
        return
    left = []
    for p in walk_files(root, (".kt", ".java", ".xml", ".kts", ".gradle",
                               ".properties", ".pro", ".json")):
        try:
            s = read(p)
        except Exception:
            continue
        if old_pkg in s:
            left.append(rel(root, p))
    if left:
        warn("改包名后仍有 %d 个文件残留 %s: %s" % (len(left), old_pkg, left[:3]))
    else:
        log("改包名: 全仓已无 %s 残留" % old_pkg)


def patch_final_clean(root, new_pkg=None):
    """收尾清理: 签名认证残留 + 内购痕迹, 一次清干净。

    顺序很关键, 必须排在最后:
      - 早于 patch_package, 改包名又会带出新的引用;
      - 早于 patch_res_languages, 刚清空的文案目录可能又被重建。
    commons 与 app 各调一次(commons 自带 strings 模块, 付费文案一大半在那)。

    刻意不去删 gplay/rustore 源集: 它们的代码根本不会进 foss 的 APK,
    开源商店扫的是 APK 而非源码树, 删了没有收益; 但 main 源集常常引用
    只在付费 flavor 里定义的符号, 一删就是一堆 unresolved reference。
    """
    log("--- 收尾清理 (%s) ---" % os.path.basename(root.rstrip("/")))
    _clean_sideload_calls(root)
    _clean_billing_manifest(root)
    _clean_payment_strings(root)
    _report_pkg_leftover(root, new_pkg)


# ------------------------------------------------------------------ main
def _preflight():
    """开跑前用一秒钟验证所有签名正则能编译、且 _anchored 幂等。

    存在的理由: 这类正则拼装错误在 Python 3.10 上只是 DeprecationWarning
    (本地跑得好好的), 到 3.12 才变成 re.error(构建机直接挂), 也就是说
    只有提交到 CI 才发现 —— 一轮构建几十分钟。这里提前炸, 成本一秒。
    """
    pats = [p for p, _ in SPEECH_STUBS] + [p for p, _ in FAKE_FUNCS] + [
        r'private fun setupUseSpeechToText\(',
        r'fun Context\.isPro\(\)',
        r'fun Long\.formatDateOrTime\(',
        r'init\s*\{',
    ]
    for p in pats:
        once = _anchored(p)
        try:
            re.compile(once)
            re.compile(_anchored(once))      # 二次前缀也必须能编译
        except re.error as e:
            warn("签名正则无法编译: %r -> %s" % (p, e))
            sys.exit(2)
    log("自检: %d 个签名正则编译通过" % len(pats))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", choices=["commons", "app"])
    ap.add_argument("root")
    ap.add_argument("--date-format", default="M-d-yyyy")
    ap.add_argument("--date-mode", choices=["full", "keep-year"], default="full")
    ap.add_argument("--commons-version", default="99.0.0-custom")
    ap.add_argument("--compile-sdk", default=None,
                    help="把 commons 的 compileSdk/targetSdk 对齐到这个值 "
                         "(应填 app 的 app-build-compileSDKVersion)")
    ap.add_argument("--locale", action="append", default=None,
                    help="语言白名单, 可重复, 默认 zh-rCN")
    ap.add_argument("--no-trim-langs", action="store_true")
    ap.add_argument("--no-locale-filters", action="store_true")
    ap.add_argument("--no-italic-fix", action="store_true")
    ap.add_argument("--no-android16", action="store_true",
                    help="不把 min/target/compile 钉到 Android 16 (API 36)。"
                         "默认开启: 装不上 Android 16 以下的设备, 换取 R8 剪掉"
                         "所有低版本兼容分支")
    ap.add_argument("--no-strip-about", action="store_true")
    ap.add_argument("--keep-settings-about", action="store_true",
                    help="保留设置页里的「关于」入口 (默认一并去掉)")
    ap.add_argument("--no-hide-datefmt", action="store_true",
                    help="保留「更改日期和时间格式」入口 (默认隐藏: 日期已全仓统一, "
                         "留着只会让人改回去)")
    ap.add_argument("--no-abi-trim", action="store_true",
                    help="不限制 ABI (默认只保留 arm64-v8a)")
    ap.add_argument("--no-device-trim", action="store_true",
                    help="不精简非手机设备资源与低密度位图")
    ap.add_argument("--package-name", default=DEFAULT_NEW_PKG,
                    help="改包名 (默认改成 %s; 传 --keep-package 保持原包名 "
                         "com.goodwy.smsmessenger)" % DEFAULT_NEW_PKG)
    ap.add_argument("--keep-package", action="store_true",
                    help="保持原包名 com.goodwy.smsmessenger 不做修改")
    ap.add_argument("--no-purchase-trim", action="store_true",
                    help="保留「购买 Thank You」卡片与「小费罐」(默认删除)")
    ap.add_argument("--no-google-trim", action="store_true",
                    help="不精简 Google 相关依赖 (默认移除 play-services 并打开 "
                         "hide_google_relations)")
    ap.add_argument("--no-sideload-trim", action="store_true",
                    help="保留签名认证弹窗 (默认去掉「应用已损坏」弹窗)")
    ap.add_argument("--no-fake-version-check", action="store_true",
                    help="保留盗版检测弹窗 (默认去掉「You are using a fake version」)。"
                         "改包名后此检测会 100%% 误判, 强烈建议保持默认")
    ap.add_argument("--no-dialog-trim", action="store_true",
                    help="保留更新日志 / 新应用推荐 / 数据访问披露三类弹窗 (默认全部去掉)")
    ap.add_argument("--no-unlock-pro", action="store_true",
                    help="不解锁付费功能 (默认打开项目支持的 UNLOCK 开关)")
    ap.add_argument("--keep-purchase-page", action="store_true",
                    help="保留 foss 的项目支持页 (默认空壳化: 入口已隐藏且无需购买)")
    ap.add_argument("--keep-speech", action="store_true",
                    help="保留语音输入功能 (默认彻底删除: 设置项 + 麦克风按钮 + 长按 + 结果回填)")
    a = ap.parse_args()
    a.hide_datefmt = not a.no_hide_datefmt
    a.abi_trim = not a.no_abi_trim
    a.device_trim = not a.no_device_trim

    _preflight()
    root = a.root
    if not os.path.isdir(root):
        log("目录不存在: %s" % root)
        sys.exit(1)
    locales = tuple(a.locale) if a.locale else ("zh-rCN",)

    if a.target == "commons":
        log("=== patch commons (%s) ===" % os.path.basename(root.rstrip("/")))
        patch_commons_version(root, a.commons_version)
        if a.compile_sdk:
            patch_sdk_align(root, a.compile_sdk)
        if not a.no_android16:
            # 放在 patch_sdk_align 之后: 即便 --compile-sdk 传了别的值,
            # 最终仍以 Android 16 为准
            patch_android16(root)
        if not a.no_unlock_pro:
            patch_ispro_always_true(root)
            if not a.keep_purchase_page:
                patch_purchase_page(root)
        if not a.keep_speech:
            patch_speech_commons(root)
        patch_constants(root, a.date_format)
        patch_baseconfig(root, a.date_format, not a.no_unlock_pro)
        patch_longkt(root, a.date_mode)
        patch_dialog(root)
        if not a.no_strip_about:
            patch_start_about(root)
            patch_about_activity(root)
        if not a.no_trim_langs:
            patch_res_languages(root)
        if not a.no_sideload_trim:
            patch_sideload_dialog(root)
        # 盗版弹窗与签名认证是两套独立检测, 必须单独开关:
        # 早先把它挂在 patch_sideload_dialog() 里, 一旦传入 --no-sideload-trim
        # 就会连带跳过, 盗版弹窗又冒出来 —— 而这恰恰是改包名后必现的那个。
        if not a.no_fake_version_check:
            patch_fake_version(root)
        if not a.no_dialog_trim:
            patch_whatsnew_commons(root)
        if not a.no_google_trim:
            patch_google_trim(root)
        if not a.no_italic_fix:
            patch_italic(root)
        patch_final_clean(root)
    else:
        log("=== patch app (%s) ===" % os.path.basename(root.rstrip("/")))
        patch_local_properties(root, os.environ.get("ANDROID_HOME"))
        if not a.no_android16:
            patch_android16(root)
        if not a.no_google_trim:
            patch_google_trim(root)
        if not a.no_purchase_trim:
            patch_purchase_card(root)
        if not a.no_dialog_trim:
            patch_app_dialogs(root)
            patch_other_group(root)
        if not a.keep_speech:
            patch_speech_app(root)
        if not a.keep_package and a.package_name:
            patch_package(root, a.package_name)
        patch_app_version(root, a.commons_version)
        patch_app_force_format(root, a.date_format)
        patch_threadactivity(root)
        if not a.no_strip_about:
            patch_menu_about(root)
            if not a.keep_settings_about:
                patch_settings_about(root)
        if a.hide_datefmt:
            patch_settings_datefmt(root)
        if not a.no_trim_langs:
            patch_res_languages(root)
            patch_lint(root)
            if not a.no_locale_filters:
                patch_locale_filters(root, locales)
        if a.abi_trim:
            patch_abi(root)
        if a.device_trim:
            patch_device_res(root)
        if not a.no_italic_fix:
            patch_italic(root)
        patch_final_clean(root, a.package_name)

    ok = verify_syntax(root)
    log("完成%s" % ("" if ok else " (但自检有问题, 见上面的 WARN)"))


if __name__ == "__main__":
    main()
