# model-service-kit 0.1.0：安装与迁移

本手册面向使用者，不依赖开发机盘符或源码工作区。交付物是普通Python wheel，不是包含Python的免安装EXE，也不会启动本地HTTP代理。首次验收范围为Windows x64／Python 3.12；元数据要求Python>=3.12，其他Python版本／平台本轮未实测。`py3-none-any`只说明本包没有平台二进制，不代表Tk及全部依赖已经跨平台验收。

## 1. 你需要拿到什么

交付目录包含：

```text
model-service-kit-0.1.0/
  model_service_kit-0.1.0-py3-none-any.whl
  README.md
  LICENSE
  docs/installation.md
  docs/integration.md
  docs/interface.md
  examples/stream_from_config.py
  open-model-config.cmd
  SHA256SUMS.txt
```

wheel只包含模型服务Python模块及包元数据，不包含NPC、测试数据、Python、模型权重、用户配置或API Key。配置GUI和调用SDK安装在同一个包里，但启动GUI并不是使用SDK的前置步骤：已有配置时主程序可以直接读取。

依赖为`httpx>=0.28,<0.29`、`keyring>=25,<26`及其传递依赖。普通安装可能联网下载依赖；**单个wheel不是完整离线环境**。图形界面还要求所选Python自带可用的Tk，Tk不能靠安装这个wheel补齐。

## 2. 安装到哪个环境

已有Python项目：使用该项目自己的Python解释器安装wheel。不要把包文件手动复制到项目源码目录，也不需要安装到磁盘根目录或全局Python。

新项目／只想使用配置GUI：在交付目录打开PowerShell，创建自己的环境。以下不要求激活脚本，因此不需要修改PowerShell执行策略：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .\model_service_kit-0.1.0-py3-none-any.whl
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -c "import model_service; from importlib.metadata import version; print(version('model-service-kit')); print(model_service.__file__)"
```

没有`py`命令时，把第一行的`py -3.12`替换为你已安装的Python 3.12解释器。应显示版本`0.1.0`，模块路径位于新环境的`Lib/site-packages/model_service/`，而不是某台开发机的源码路径。

若pip报告Python版本不满足要求，先检查实际执行的是哪个Python；若提示不能连接包索引，检查依赖下载条件，不要改成安装PyPI上同名的未知项目。本版本只交付本地wheel，**未上传PyPI**。

## 3. 启动配置GUI

```powershell
.\.venv\Scripts\python.exe -m model_service.gui
```

也可直接运行安装器生成的`.\.venv\Scripts\model-service-config.exe`，或双击交付目录里的`open-model-config.cmd`。后者依次使用`MODEL_SERVICE_PYTHON`指定的解释器、交付目录旁的`.venv/Scripts/python.exe`、PATH中的python；不会自动回到开发者工作区。使用其他项目环境时，可把`MODEL_SERVICE_PYTHON`设为该解释器的完整路径，或直接给已安装环境中的入口创建桌面快捷方式。

pip在Windows生成的`model-service-config.exe`只是该Python环境的命令入口，**不是独立免安装程序**。移动或复制整个venv到另一台设备不是安装方式；请在目标位置重新创建环境、安装wheel。源码和交付目录的启动脚本均只使用显式解释器、本目录venv或PATH，不寻找其他项目的环境。

只想试界面、不碰已有配置，可显式指定新路径：

```powershell
.\.venv\Scripts\python.exe -m model_service.gui --config .\trial-profiles.json
```

启动、保存和选择活动模型不发起模型请求。只有确认“连接测试”或由你的程序显式调用SDK才会请求服务，网络模型可能计费。GUI填写规则及诊断说明见[使用手册](../README.md#gui使用)。

## 4. 配置与密钥放在哪里

- 默认普通配置位于当前系统用户的`model-service-kit/profiles.json`；Windows通常在`LOCALAPPDATA`下。窗口顶部显示实际路径，也可用`--config`指定文件。
- **venv隔离不等于配置隔离。** 同一系统用户下的不同项目／venv默认会使用同一个配置文件；需要隔离时，GUI及程序都显式指定同一份独立文件。
- 服务地址、模型名称、参数属于普通配置，会以明文保存；API Key不在普通配置中，只保存系统凭据库或当前进程凭据的引用。
- 独立GUI保存网络密钥后供其他进程调用，应选“系统凭据库”，并使用同一系统用户。“仅本次进程”的密钥不跨进程共享，重开程序后必须重新提供。
- 换设备可以复制普通配置文件，但凭据引用不会带走密钥；在目标设备重新填写API Key。复制配置前检查服务地址等元数据是否也需要保密。wheel、说明书和示例不提供任何用户的真实配置。
- 系统凭据库由操作系统提供保护；运行时密钥仍在进程内存，不承诺抵御同用户权限的恶意程序。无法访问凭据时明确报错，不静默改为无认证或明文文件。

已有schema_version=2配置无需因安装0.1.0而迁移。旧明文格式需在GUI中显式确认迁移；本次安装不会自动读取、改写或删除旧文件／凭据。升级不会自动替换活动模型或改变已取得的调用快照。

## 5. 在其他项目里调用

先把wheel安装到**运行那个项目**的Python环境，再按[接口接入与旧调用改写](integration.md)接入。你的程序继续负责提示词、历史消息、JSON解析和业务状态；本包不接管这些逻辑。

交付示例默认只预览，读取配置元数据但不读取API Key、不发送请求：

```powershell
.\.venv\Scripts\python.exe .\examples\stream_from_config.py --config .\trial-profiles.json
```

先在GUI保存并设为活动配置；也可添加`--profile 你保存的配置标识`选择非活动模型。确认目的地、要发送的消息和费用后，添加`--send`才发起一次真实请求；不会自动重试或切换模型。

## 6. 升级、卸载与离线准备

升级时在同一目标环境安装新的本地wheel；先关闭使用旧版本的GUI和业务程序，避免在途调用与文件替换交错。不要把`pip install -e`当作分发安装。卸载命令只移除这个环境中的包，不删除用户配置或系统凭据：

```powershell
.\.venv\Scripts\python.exe -m pip uninstall model-service-kit
```

需要离线安装时，在与目标Windows／Python版本和架构匹配的联网环境准备依赖wheel，再复制整个wheelhouse到目标机器：

```powershell
python -m pip download --only-binary=:all: --dest wheelhouse .\model_service_kit-0.1.0-py3-none-any.whl
python -m pip install --no-index --find-links .\wheelhouse .\model_service_kit-0.1.0-py3-none-any.whl
```

这些命令中的`python`仍须换成你要使用的环境解释器。本次交付不默认打包依赖wheelhouse、Tk、Python或模型权重。

打包／venv说明依据[PyPA打包指南](https://packaging.python.org/en/latest/tutorials/packaging-projects/)和[pip可复现安装说明](https://pip.pypa.io/en/stable/topics/repeatable-installs/)。本包0.1.0与NPC项目V0.1.5独立编号，构建wheel不等于公开发布或M1验收。
