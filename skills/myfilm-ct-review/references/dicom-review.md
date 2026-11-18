# 本地 CT 校验与测量

Python 3.10+。复用现有 `numpy`、`pydicom`、`scipy` 和 `Pillow`。缺少时可将虚拟环境建在任务工作区，用其解释器运行 `python -m pip install --no-cache-dir -r scripts/requirements.txt`。压缩 DICOM 需视 Transfer Syntax 使用 pydicom 支持的解码插件。命令使用虚构文件名和帧数，输入和输出均用授权检查的私有工作目录。

```powershell
python scripts/ct_review.py inventory "source.zip"
python scripts/ct_review.py prepare "source.zip" --group 1 --expected-count 64 --out "work/review-A" --ww 1800 --wl 450
python scripts/ct_review.py distance "work/review-A/geometry.json" --a 30 150 120 --b 31 152 125
```

- `inventory` 读 ZIP、单文件或目录（可含多个 ZIP 与裸 DICOM），以 SeriesInstanceUID 私下分组。输出只含组号、序列号、图像数及矩阵尺寸。用网页或本地阅片器私下核对部位、侧别和序列描述，再与清点结果相匹配。组号只对本次输入有效。文件名、InstanceNumber、SeriesNumber 和 ZIP 次序都不能作为物理层序。
- `prepare` 按 IPP 投影到 IOP 法向排序，检查方向、帧、间距、重复图像、尺寸和是否能用单仿射表达所有层。保留恒定倾斜的真实三维层步进；遇非均匀、缺字段、重复位置、定位像、多帧及不支持的格式不强行补。逐层解码及应用 rescale，只在可确认单位时标 HU。下载齐全由独立网页计数加强，单凭等间距不能证明首尾未漏。
- 数组轴为零起始 `[k,r,c]`，`geometry.json` 保存到 DICOM LPS 毫米的变换、实际层中心距、原始层厚、误差和原生 InstanceNumber 的排序。MPR PNG 是分别沿 LPS 正交轴重切的一处截面，三图同中心，`--center K R C` 可定位到关注点。图中方向是图像左到右／上到下。`mpr` 字段含像素到 LPS 的映射。实际打开至少一幅重切图并回到原生层核对方位和结构。插值只为显示，不能提高原始采样分辨率。记录量化时应保留原片。
- `distance` 输入数组坐标，输出两端 LPS 点和欧氏距离。明确端点是亚像素估计还是原生像素中心。任何台阶、狭窄或间隙定量仍需先确认解剖对象、正确测量定义和成像局限。报告应附两端、原生层、复核切面、方向、窗口、插值与投影厚度，不给未验证解剖配对赋假精度。
- 需连续滚动、斜位交互、多帧或复杂几何时，用已有的本地医学阅片器，例如 3D Slicer 的 DICOM 和 Markups，使用任务独立数据库。Slicer 的世界坐标通常为 RAS，换成 LPS 要对前两轴取反并检查方向。软件提示应逐条核实，不能用导入完成代替空间校验。
- 原始资料和衍生 PNG/NPY 仍可包含真实病例信息。不要复制到技能目录、通用测试包、共享链接或日志。避免回显未筛选的 DICOM 标签和错误堆栈。

技术依据：[DICOM 平面几何](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/sect_C.7.6.2.html)、[CT 像素值](https://dicom.nema.org/medical/dicom/current/output/chtml/part03/sect_C.8.2.html)、[pydicom 像素解码](https://pydicom.github.io/pydicom/stable/guides/user/image_data_handlers.html)、[Slicer DICOM](https://slicer.readthedocs.io/en/latest/user_guide/modules/dicom.html)、[DICOM 保密规范](https://dicom.nema.org/medical/dicom/current/output/chtml/part15/chapter_E.html)。
