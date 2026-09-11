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
    签名和真的一模一样, 直接 re.search 会命中注释, 后面的大括号配平全乱。"""
    out = list(src)
    n = len(src)
    i = 0
    while i < n:
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j + 3
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
        elif src[i] == '"':
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == '"':
                    i += 1
                    break
                if src[i] == "\n":
                    break
                for k in range(i, i + 1):
                    if out[k] != "\n":
                        out[k] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


def fun_span(src, sig_pattern):
    """按签名定位函数体: 返回 (body_start, body_end)。
    body_start 是 '{' 的下标, body_end 是配对的 '}' 的下标。"""
    if not sig_pattern.startswith("^"):
        # 只允许行首到 fun 之间是空白和修饰符, 保证命中真函数而不是注释/字符串
        sig_pattern = r"(?m)^[ \t]*" + MODIFIERS + sig_pattern
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


def drop_fun(src, sig_pattern):
    """整段删除一个顶层函数(含签名与函数体), 用于移除波斯历分支函数。
    返回 (new_src, ok)。"""
    if not sig_pattern.startswith("^"):
        sig_pattern = r"(?m)^[ \t]*" + MODIFIERS + sig_pattern
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

    if int(sdk) < 37:
        s, k3 = re.subn(r'(?m)^(androidx-lifecycle\s*=\s*)"[^"]*"',
                        r'\1"2.10.0"', s)
        if k3:
            log("commons: androidx-lifecycle 压回 2.10.0 (2.11.0 要求 compileSdk>=37)")

    if s != orig:
        write(p, s)
        log("commons: compileSdk/targetSdk 已对齐为 %s" % sdk)


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


def patch_baseconfig(root, fmt):
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

    # 4) 签名/侧载认证: appSideloadingStatus 恒为 FALSE。
    #    这是"应用已损坏, 请从商店重新下载"弹窗的源头开关。
    src, k4 = re.subn(
        r'(var appSideloadingStatus: Int\s*\n\s*get\(\)\s*=\s*)[^\n]*',
        r'\1SIDELOADING_FALSE', src)

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
        log("BaseConfig.kt: appSideloadingStatus 恒为 SIDELOADING_FALSE (签名认证弹窗关闭)")
    else:
        warn("BaseConfig.kt: 未匹配到 appSideloadingStatus")


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

    # 注意: 不要去注释 showSideloadingDialog() 的"调用点"。
    # Activity.kt 里有 `fun Activity.showSideloadingDialog() {` 这个定义,
    # 朴素正则会连它一起注释掉 -> fun Activity.// xxx { -> 语法错误。
    # 而且入口已经空壳化(直接 callback), 调用它本身是无害的, 不需要动。


# ------------------------------------------------------------------ 3. 语言精简
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


# ------------------------------------------------- 9. 购买 Thank You / 内购
PURCHASE_KEYS = ("purchase", "thank_you", "thankyou", "donate",
                 "contribute", "support_us", "become_", "tip_jar")


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
    """把应用内所有斜体改回默认正体:
       XML: android:textStyle="italic" / "bold|italic" / <item name="android:textStyle">italic</item>
       KT : Typeface.ITALIC / Typeface.BOLD_ITALIC"""
    n_xml = n_item = n_kt = 0

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
        if s != orig:
            write(p, s)
            n_kt += k

    log("斜体 -> 正体: XML 属性 %d 处, style item %d 处, Kotlin Typeface %d 处"
        % (n_xml, n_item, n_kt))
    if n_xml + n_item + n_kt == 0:
        warn("一处斜体都没匹配到 —— 如果该版本确实有斜体, 请检查是否被写成自定义 font 或 span")


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


# ------------------------------------------------------------------ main
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
    ap.add_argument("--package-name", default=None,
                    help="改包名, 如 com.mycompany.sms (默认不改, 保持 "
                         "com.goodwy.smsmessenger)")
    ap.add_argument("--no-purchase-trim", action="store_true",
                    help="保留「购买 Thank You」卡片与「小费罐」(默认删除)")
    ap.add_argument("--no-google-trim", action="store_true",
                    help="不精简 Google 相关依赖 (默认移除 play-services 并打开 "
                         "hide_google_relations)")
    ap.add_argument("--no-sideload-trim", action="store_true",
                    help="保留签名认证弹窗 (默认去掉「应用已损坏」弹窗)")
    a = ap.parse_args()
    a.hide_datefmt = not a.no_hide_datefmt
    a.abi_trim = not a.no_abi_trim
    a.device_trim = not a.no_device_trim

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
        patch_constants(root, a.date_format)
        patch_baseconfig(root, a.date_format)
        patch_longkt(root, a.date_mode)
        patch_dialog(root)
        if not a.no_strip_about:
            patch_start_about(root)
            patch_about_activity(root)
        if not a.no_trim_langs:
            patch_res_languages(root)
        if not a.no_sideload_trim:
            patch_sideload_dialog(root)
        if not a.no_google_trim:
            patch_google_trim(root)
        if not a.no_italic_fix:
            patch_italic(root)
    else:
        log("=== patch app (%s) ===" % os.path.basename(root.rstrip("/")))
        patch_local_properties(root, os.environ.get("ANDROID_HOME"))
        if not a.no_google_trim:
            patch_google_trim(root)
        if not a.no_purchase_trim:
            patch_purchase_card(root)
        if a.package_name:
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

    ok = verify_syntax(root)
    log("完成%s" % ("" if ok else " (但自检有问题, 见上面的 WARN)"))


if __name__ == "__main__":
    main()
