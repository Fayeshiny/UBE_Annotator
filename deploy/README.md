# 服务器部署与运维

## 本机正式实例（2026-10-20）

- 计划公网入口：`https://ubuntu.tail10b45c.ts.net`，**目前等待 Tailscale 网络所有者启用 Funnel，尚未验证上线**。
- 后端：`http://127.0.0.1:8002`，只监听本机，由 HTTPS 入口转发；不能直接从其他电脑访问 8002。
- 服务：当前用户的 `ssi-annotator.service`，单进程、无开发热重载，故障自动重启。
- 项目：`/mnt/nvme1n1/yefengyu/SSI/annotator`
- 数据：项目下 `data-production/`；原 `data-dev/` 没有迁移或覆盖。
- 配置：`deploy/production.env`，只允许当前用户读取，不进入 Git。
- 初始管理员：`admin`，随机密码保存在 `deploy/admin-credentials.txt`（权限 0600）。
- SAM：配置为 `sam3.1`，物理 GPU 2，复用本机权重；模型按需加载。本轮上线验证不包含真实模型推理。

本次先验证了内网 HTTP，再按公网 HTTPS 要求将后端收回本机并启用 `SSI_COOKIE_SECURE=1`。
本机 HTTP 仅供健康检查/代理，浏览器登录请使用最终 HTTPS 入口。
已修复静态文件路由的目录穿越漏洞并验证：编码路径及逃逸符号链接无法读取构建目录外文件。
初始管理员密码已更新。没有修改防火墙、购买域名或开通付费服务。

## 从零启用公网 HTTPS

服务器已经登录 Tailscale，具有 `ubuntu.tail10b45c.ts.net` 域名，`tailscaled` 已自启动。
Funnel 可转发公网 HTTPS 到本机后端，不需要路由器端口映射，也不要求标注者安装 Tailscale。

1. 由该 Tailscale 网络所有者打开 CLI 提示的管理链接，登录并启用 Funnel：
   `https://login.tailscale.com/f/funnel?node=nkGbtPSZhF11CNTRL`。
2. 服务器执行下列命令，`--bg` 将入口保存在 Tailscale 后台配置中：

   ```bash
   tailscale funnel --bg --https=443 http://127.0.0.1:8002
   tailscale funnel status
   curl --fail https://ubuntu.tail10b45c.ts.net/api/health
   ```

3. 在标注者电脑打开 HTTPS 地址，验证登录、视频跳转及保存。证书由 Tailscale 管理。

停止这个公网入口可执行 `tailscale funnel --https=443 off`，后端与已有数据会保留。
如没有 Tailscale 管理员账号，需要由服务器/网络管理员处理此步骤。

**带宽限制：** [Tailscale 官方说明](https://tailscale.com/kb/1223/funnel)明确指出 Funnel 有不可自行调整的带宽限制。
它可以先用于上线试用，但多人同时查看大体积视频时需要实测，不能承诺低延迟。
正式规模扩大后，可使用自有域名与具有足够带宽的公网 HTTPS 反向代理；
当前应用和独立数据目录可以继续使用，只需替换入口。已有 Tailscale 客户端的标注者也可以评估私有 HTTPS Serve 入口。

首次登录后，在“用户管理”设置管理员个人密码、创建每位标注者的独立账号。
然后导入已脱敏的视频，等待处理完成，并分配任务。
正式库初始只有管理员，没有病例或标注任务。密码文件用后删除，避免长期保存明文密码。

## 启停与检查

```bash
systemctl --user status ssi-annotator.service
systemctl --user restart ssi-annotator.service
systemctl --user stop ssi-annotator.service
journalctl --user -u ssi-annotator.service -n 100 --no-pager
curl --fail http://127.0.0.1:8002/api/health
loginctl show-user "$USER" -p Linger
```

已启用 `Linger=yes` 和服务自启动，退出 SSH 后仍继续运行，重启服务器后由用户服务管理器启动。
GPU、网络及数据盘应在使用前就绪；本轮没有重启整台服务器进行验证。
不要给本应用增加多个 Uvicorn worker：SAM 会话与导入任务保存在进程内。
正式脚本通过数据目录锁避免重复启动；开发实例应始终使用单独的数据目录和端口。

## 更新

在项目目录执行，更新前先安排停止标注并备份：

```bash
backend/.venv/bin/python -m pytest backend/tests -q
cd frontend
node --test tests/*.test.mjs
npm run build
cd ..
systemctl --user restart ssi-annotator.service
curl --fail http://127.0.0.1:8002/api/health
```

前端直接由后端提供构建文件，不需要运行 Vite 开发服务器。
重启会中断正在导入的视频和 SAM 会话，避免在导入、传播或未保存标注时更新。

## 备份与恢复

备份包含数据库、病例视频、标注、掩膜及导出文件，排除可再生成的缓存。
脚本短暂停服，完成一致性快照后自动恢复原来的运行状态；备份期间不要启动其他写入相同数据目录的工具。
备份文件位置必须在正式数据目录外，且不能覆盖已有文件。

```bash
mkdir -p data-backups
chmod 700 data-backups
./scripts/backup-production.sh "$PWD/data-backups/ssi-$(date +%Y%m%d-%H%M%S).tar"
```

本机备份不能应对磁盘故障，定期复制到授权的内网备份存储。
另行妥善备份 `deploy/production.env`；管理员密码文件不应作为长期备份的一部分。

恢复时停止服务，把归档解压到一个**新的空目录**，确认 `ssi.sqlite3` 与 `cases/` 同时恢复，
将 `production.env` 中 `SSI_DATA_DIR` 改为该目录后启动。保留旧目录直到验证完成。
不要在运行时直接覆盖 SQLite 文件，也不要仅恢复数据库而遗漏对应病例文件。

## 视频跳帧速度

代码核查结果：

- 普通导入已转码为 H.264，关键帧间隔 15 帧，并启用 MP4 faststart；无需再次盲目缩短间隔。
- `/api/cases/{case_id}/video` 通过登录和任务权限检查后返回视频，支持 HTTP Range / 206。
- 工作区已有跳转队列，连续请求只保留最新目标，减少重复解码；空掩膜时跳过大幅 RGBA 分配。
- 暂停跳帧已改为加载目标帧图片，连续播放保留视频；视频仅预读元数据，避免与图片争抢带宽。
- 新增采样点预生成、相邻帧预取、浏览器解码缓存和服务器磁盘缓存，完整机制、配置和实测结果见
  [帧跳转与缓存](frame-performance.md)。

现有开发库最长病例的实际播放文件约 2.51 GB，1080p、约 60 fps，关键帧平均间隔 0.58 秒。
本机模拟网络的实测结果已记录，但尚未对标注者的实际公网网络测量延迟。

部署后在标注者电脑上，用浏览器 Network 面板观察视频请求：

1. 随机跳转应返回 `206 Partial Content`，若使用代理，确认 Range 和 Content-Range 没有被破坏。
2. 下载耗时高时，优先评估内网/VPN 带宽及并发人数。
3. 暂停跳帧现在请求 `/api/cases/{case_id}/frame/{frame_idx}.jpg`，可通过响应头 `X-Frame-Cache` 检查服务器缓存命中。
   首次未命中的帧仍需抽帧；后台预生成完成后再测量日常标注体验。

## 另一台服务器安装

### Ubuntu 22.04 / Google Cloud 一键配置环境

在 VM 上以运行平台的普通用户执行（不要使用 `sudo bash`）：

```bash
git clone https://github.com/Fayeshiny/UBE_Annotator.git
cd UBE_Annotator
bash scripts/setup-environment.sh --sam31
```

脚本通过 apt 安装 FFmpeg、FFprobe、编译工具等系统依赖（需要 sudo），
通过 [uv](https://docs.astral.sh/uv/) 安装独立 Python 3.12 和 `backend/.venv`，
通过 [nvm](https://github.com/nvm-sh/nvm) 安装 Node.js 22，安装后端并构建前端。
`--sam31` 额外安装 CUDA 12.8 的 PyTorch 2.10.0 / torchvision 0.25.0、
固定提交的 SAM 源码及推理依赖，并检查 CUDA 张量运算。
不需要真实分割时可以省略 `--sam31`；管理员已装好系统工具时可加 `--skip-system`。

首次运行创建仅当前用户可读的 `deploy/production.env`，默认监听 `127.0.0.1:8002`、
使用 mock 分割。已有配置、数据库和权重不会被覆盖。脚本不创建管理员、不启动服务、
不修改防火墙，也不下载模型权重。若已有正式服务运行，会要求先停服。
安装依赖需要访问 Ubuntu 软件源、Astral、GitHub、npm、PyPI；SAM 还需要 PyTorch 下载源。

SAM 3.1 权重需单独取得（主 README 提供下载方式），然后编辑配置：

```bash
SSI_SAM_BACKEND=sam3.1
SSI_SAM_CHECKPOINT=/absolute/path/to/sam3.1_multiplex.pt
CUDA_VISIBLE_DEVICES=0
SSI_SAM_USE_FA3=0
```

单 GPU VM 使用 GPU 0，不能照搬旧服务器 GPU 2 的配置。
Git 克隆不包含旧服务器的数据库、病例视频、模型权重或私有配置；
如需迁移，请先按备份与恢复流程处理，再启动服务。

环境安装并取得权重后，也可以用一个脚本配置 SAM 3.1 并启动持久服务：

```bash
bash scripts/start-production-sam31.sh
# 权重不在默认目录时：
bash scripts/start-production-sam31.sh --checkpoint /absolute/path/to/sam3.1_multiplex.pt --gpu 0
```

默认寻找 `models/sam3.1/sam3.1_multiplex.pt`，或沿用已有配置中的权重路径和 GPU。
脚本检查 CUDA、前端构建及端口，更新 `deploy/production.env` 中的 SAM 设置，
保留已有数据目录、监听地址、端口及 HTTPS cookie 设置。
首次交互式创建 `admin`；已有同名账号不会改密码。默认使用独立的 `data-production/`，
不会自动迁移 `data-dev/` 的账号和病例。
脚本安装并启用 systemd 用户服务，必要时通过 sudo 启用 linger，最后检查健康接口。
运行后退出 SSH 或重启 VM，服务仍会自动启动；SAM 模型按需加载，健康检查不代表推理已经验证。
若服务已在运行，脚本会退出；需要更新配置时，先在无标注/导入/推理任务时执行
`systemctl --user stop ssi-annotator.service`，再运行脚本。
脚本不会替换属于其他代码目录的同名服务。

也可以手动执行下面的步骤：

新建空库并启动：

```bash
set -a
source deploy/production.env
set +a
backend/.venv/bin/ssi-annotator init --admin admin
bash scripts/install-production-service.sh
sudo loginctl enable-linger "$USER"
systemctl --user start ssi-annotator.service
curl --fail http://127.0.0.1:8002/api/health
```

先通过 VS Code 的 Ports 面板转发 VM 的 8002 端口，在本机访问转发后的 HTTP 地址。
这时保留 `SSI_COOKIE_SECURE=0`；正式 HTTPS 入口配置完成后改为 `1` 并重启服务。

### 手动安装

需要 Python ≥3.11、Node ≥20、FFmpeg、`flock` 和用户 systemd。
按主 README 安装后端及需要的 SAM 依赖，构建前端，然后：

```bash
cp deploy/production.env.example deploy/production.env
chmod 600 deploy/production.env
# 编辑绝对数据路径、监听地址、端口、SAM 和 HTTPS cookie 配置。
set -a
source deploy/production.env
set +a
backend/.venv/bin/ssi-annotator init --admin admin
./scripts/install-production-service.sh
# 若当前用户尚未启用 linger，由管理员执行：
sudo loginctl enable-linger "$USER"
systemctl --user start ssi-annotator.service
```

`production.env` 是由 Bash 读取的受信任配置，不应允许其他用户写入。
