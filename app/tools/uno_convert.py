"""通过 UNO 让常驻的 LibreOffice 转换一个文件。由 lo_resident 以系统 Python（能 import uno）运行：

    python3 uno_convert.py 管道名 输入文件 输出文件 过滤器名 过滤器参数JSON 连接等待秒数

退出码：0 成功；3 连不上常驻实例；4 打开文档失败；5 保存失败；其他为未预期的错误。
这个脚本只是客户端，文档的解析和转换都在常驻实例里进行。
"""
import json
import sys
import time

import uno  # type: ignore
from com.sun.star.beans import PropertyValue  # type: ignore


def pv(name, value):
    p = PropertyValue()
    p.Name = name
    p.Value = value
    return p


def typed(spec):
    """把 --convert-to 的 JSON 写法 {"type": "boolean", "value": "true"} 换成 Python 值。"""
    t, v = spec.get("type"), spec.get("value")
    if t == "boolean":
        return str(v).lower() == "true"
    if t in ("long", "short", "int"):
        return int(v)
    if t in ("double", "float"):
        return float(v)
    return str(v)


def main(argv):
    pipe, src, dst, filt, data_json, wait = argv
    data = json.loads(data_json) if data_json else {}
    local = uno.getComponentContext()
    resolver = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
    deadline = time.time() + float(wait)
    while True:
        try:
            ctx = resolver.resolve(f"uno:pipe,name={pipe};urp;StarOffice.ComponentContext")
            break
        except Exception:
            if time.time() > deadline:
                sys.stderr.write("连不上常驻 LibreOffice\n")
                return 3
            time.sleep(0.1)
    desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    # MacroExecutionMode 0 = NEVER_EXECUTE，UpdateDocMode 0 = NO_UPDATE（不更新外部链接）
    load = (pv("Hidden", True), pv("MacroExecutionMode", 0), pv("UpdateDocMode", 0))
    try:
        doc = desktop.loadComponentFromURL(uno.systemPathToFileUrl(src), "_blank", 0, load)
    except Exception as e:
        sys.stderr.write(f"打开失败：{e}\n")
        return 4
    if doc is None:
        sys.stderr.write("打开失败\n")
        return 4
    try:
        props = [pv("FilterName", filt), pv("Overwrite", True)]
        if data:
            fd = tuple(pv(k, typed(v)) for k, v in data.items())
            props.append(pv("FilterData", uno.Any("[]com.sun.star.beans.PropertyValue", fd)))
        uno.invoke(doc, "storeToURL", (uno.systemPathToFileUrl(dst), tuple(props)))
    except Exception as e:
        sys.stderr.write(f"保存失败：{e}\n")
        return 5
    finally:
        try:
            doc.close(True)
        except Exception:
            try:
                doc.dispose()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
