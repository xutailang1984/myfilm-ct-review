# MyFilm CT 复核技能

把你有权查看的 MyFilm 云胶片链接交给本地智能助理。这个 Agent Skill 可以走网站正常流程验证分享、选择原始 CT 序列、下载 DICOM，然后在本地检查文件是否完整并生成三平面图。已经有 DICOM ZIP 或目录时，可以直接在本地开始。

## 安装一次，之后用对话调用

**Codex 本地版**可以把下面这句话发给助理：

```text
请用 $skill-installer 安装这个公开技能，并告诉我怎样调用它：
https://github.com/xutailang1984/myfilm-ct-review/tree/main/skills/myfilm-ct-review
```

**Claude Code 本地版**或其他兼容 Agent Skills 的助理可以把完整的 [`skills/myfilm-ct-review/`](skills/myfilm-ct-review/) 导入自己的技能目录。Claude Code 的个人目录是 `~/.claude/skills/`，导入后可用 `/myfilm-ct-review`。Codex 也可使用客户端支持的个人或项目技能目录。请复制整个技能目录，让 `SKILL.md` 和 `scripts/`、`references/` 保持相对位置。[Codex 技能说明](https://developers.openai.com/codex/skills) · [Claude Code 技能说明](https://code.claude.com/docs/en/skills) · [Agent Skills 规范](https://agentskills.io/specification)

**安装好后**，在与助理的私密对话中这样说：

> 用 myfilm-ct-review 复核我有权查看的这份 CT。重点看腰椎，先确认原始序列和层数，再生成三平面复核图。如果序列无法区分，告诉我有哪些候选。

同一条私密消息可以附上分享链接及分享码，或本地 DICOM 文件路径。不要把这些信息或影像放进 GitHub Issue、公开评论或公开仓库。

## 支持范围

- 网页流程在 **Windows 和 Chrome** 的隔离会话中验证。需要助理能运行 Python 3.10+、访问本地工作目录并访问授权的 MyFilm 分享。纯聊天客户端只有文件阅读能力时，可以解释用法，运行脚本需要有相应权限的环境。
- 当前按部位筛选支持**左膝、右膝和腰椎**。只有唯一可信候选才自动下载。同一部位如果有不同重建、标签矛盾或来源不明，会保留候选供复核。已确认序列号时也可直接指定。
- 通过 MyFilm 可见的验证表单和导出功能取片。医院权限、分享有效期和网页布局会影响可用性。网页取片失败时也可以使用已经授权保存的原始 DICOM。
- 上传到仓库的内容只有通用说明和代码。运行过程中原片及衍生图应保存在使用者控制的私有工作区。在云端助理里运行时，确认工作区和文件访问范围符合你的隐私要求。

技能生成的图像和毫米距离用于辅助定位和复核。影像是否构成诊断或影响治疗，仍需由临床人员结合病史和查体判断。

## 本地脚本

助理可在安装后的 `myfilm-ct-review` 目录中，用任务专用 Python 环境执行。如果只做离线复核，请安装 `scripts/requirements.txt`。网页取片使用 `scripts/requirements-web.txt` 和已安装的 Chrome。

```text
python -m pip install -r scripts/requirements-web.txt
python scripts/myfilm_fetch.py fetch --target 腰椎 --out work/private-case/original.zip
python scripts/ct_review.py inventory work/private-case/original.zip
```

先创建私有输出目录。脚本会在交互终端不回显地读取分享链接和分享码。助理也可以通过私有标准输入传入。完整命令、筛选规则和故障处理见 [网页取片](skills/myfilm-ct-review/references/myfilm-automation.md)及[本地复核](skills/myfilm-ct-review/references/dicom-review.md)。

这是独立项目。代码以 [MIT 许可证](LICENSE) 发布。
