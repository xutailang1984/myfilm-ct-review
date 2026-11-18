# 通过脚本获取授权的 MyFilm 原始 CT

`scripts/myfilm_fetch.py` 使用独立的临时 Chrome 会话，通过医院分享页的正常表单、DICOM 信息窗口和右键导出功能接收 ZIP。它不调用 Computer Use。还没有核实 MyFilm 对普通分享开放的正式 API，网页切换控件仍是这条路径的依赖。[MyFilm 公开页](https://www.myfilm.cc/)、[Playwright 浏览器和下载文档](https://playwright.dev/python/docs/downloads)

## 执行方式

- 已有校验过的原始 DICOM 时直接使用本地文件。只有需要首次获取或补原片时才运行网络脚本。使用 Python 3.10 及以上版本、Playwright，以及已装的 Chrome。脚本也提供 `--browser msedge`，验收使用 Chrome。
- 将依赖安装在任务专用环境。可用该环境的解释器运行 `python -m pip install --no-cache-dir -r scripts/requirements-web.txt`。脚本的下载临时目录由调用方指定在任务磁盘，退出时自动清理，最终 ZIP 保留在 `--out`。
- 目标是左膝、右膝或腰椎时直接用 `fetch --target`。脚本读取候选的部位字段、扫描协议、可用侧别、重建核及实际切面方向，仅选择证据一致且唯一的经典原始轴向 CT。可先用 `catalog --target --work` 查看候选和理由。禁止对部位模式同时提供序列号、层数或网页序号，以免掩盖部位依据不足的问题。
- 同一膝关节如有多个原始重建，只有用户在当前请求或已有任务里明确要看骨折细节、骨皮质等骨性结构时，才可加 `--detail sharp`。明确要用软核时才加 `--detail soft`。重建核无法识别、证据矛盾或有多个仍适用的序列时，保留候选和差异。默认不会凭层数、序列号、顺序、坐标符号或窗宽窗位选一套。
- 已确认其他部位的序列时，仍能用 `fetch --series-number`。已知网页张数时加 `--frames`，同序列号跨检查冲突时用 `--item` 缩小范围。其他部位、未知标签、页面结构变化、验证码表单变化、浏览器安全告警或医院未开放导出时，按网页手动路径核对。

```powershell
python scripts/myfilm_fetch.py catalog --work work/private-case
python scripts/myfilm_fetch.py catalog --target 腰椎 --work work/private-case
python scripts/myfilm_fetch.py fetch --target 腰椎 --out work/private-case/original.zip
python scripts/myfilm_fetch.py fetch --target 左膝 --detail sharp --out work/private-case/bone-detail.zip
```

目录须提前创建。上例不传公开命令行凭据，交互终端会无回显地询问链接和分享码。智能体代运行时，给进程加 `--input-stdin`，通过进程私有标准输入直接传一行 JSON，字段名为 `url` 和 `code`。不要把实际值写进命令行参数、脚本、连接器配置、日志或示例。若会话工具支持子进程，设标准输入为管道，写完立即关闭。脚本只在授权的正常分享页验证一次，授权失败时检查输入来源，勿循环尝试。

## 判断是否完成

1. `authorized_viewer_open` 表示授权页面及影像区打开。`target_series_verified` 表示已从网页 DICOM 信息确认目标，并独立读取了网页帧数。
2. `browser_zip_saved` 表示浏览器已正式交付文件。它可能要等网页组包几分钟。只有 `validated_dicom_zip` 且进程返回 0 才能把新 `--out` 作为可用原片交接。脚本会核对 ZIP CRC、网页与 ZIP 序列身份、帧数、重复实例、空间几何和逐层解码。部位模式额外逐层核对部位、侧别、原始类型、切面及用户明确指定的核类别。不以网络分包数作为验收依据。
3. 完成后将原片交给 `ct_review.py` 做需要的本地重建及复算。如需确认医院网页以外的部位、侧别或具体测量，再按相应的复核路径检查。脚本未提供临床自动定级。

在 Windows 的隔离 Chrome 中验证过普通授权分享、官方导出和逐层解码。验收以实际下载 ZIP 与基准原片的逐文件内容为依据。

## 部位证据怎么使用

正式部位或扫描协议识别部位，侧别使用 DICOM 字段或紧邻 `KNEE` 的明确左右词；例如 `L SPINE` 表示腰椎。序列描述用于交叉核对，单靠描述不能自动选定。查看方向用图像方向余弦。源数据里的核标签用于区分可识别的锐、软类别，不把调骨窗当成更好分辨率。标签无法解释、多个可用重建或来源矛盾时停在候选。此路径支持常规单帧 CT。遇到 Enhanced CT 多帧 DICOM 或仅有编码解剖字段的布局时，先用支持该格式的阅片器确认。[DICOM 系列属性](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/sect_C.7.3.html)、[图像平面属性](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/sect_C.7.6.2.html)

选择规则覆盖唯一部位匹配、相同层数的对侧序列、同侧多套重建和元数据冲突。即使有一个已识别的匹配，只要还有无法排除的候选，也会停在候选阶段。真实影像与凭据不随技能分发。
