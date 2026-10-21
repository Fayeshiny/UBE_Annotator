# 约 5～10 秒播放失败与无效 HTTP 请求

2026-10-21 检查 `data-dev/cache/http-protocol-errors.jsonl`：16 条记录中，最近 15 条均为 `HttpParserInvalidMethodError`，输入 13 字节、首字节 `0x09`。现场连接 8001 的三个回环连接均来自 VS Code Server（PID 4013828，版本提交 `6d62b1d5dace93a33b3a6d3eca1317bd5e15bcb1`）。

该版本源码 `src/vs/base/parts/ipc/common/ipc.net.ts` 中，PersistentProtocol 的心跳类型为 9，头长 13 字节，每 5000 ms 发送一次。心跳结构为 `type:u8, id:u32be, ack:u32be, payload_length:u32be`，其中 id 与 payload_length 均为零。源码链接：

https://github.com/microsoft/vscode/blob/6d62b1d5dace93a33b3a6d3eca1317bd5e15bcb1/src/vs/base/parts/ipc/common/ipc.net.ts

这些证据强烈指向 VS Code 转发中有控制消息进入了原始 HTTP 连接。已有真实记录只保存首字节和长度，尚不能证明剩余字节匹配完整心跳，亦未记录当时是否正发送视频，因此真实链路的因果关系仍待确认。

隔离集成测试使用当前安装的 Uvicorn/httptools：启动一个未完成的 206 视频响应，再向同一连接注入完整心跳格式，确实产生 InvalidMethod、写入 400 并关闭连接，视频长度未收齐。这解释了此类消息为何可能导致媒体加载失败；该测试并非用户实际链路复现。

## 已增加的诊断

完整匹配 13 字节心跳结构后，警告显示 `protocol=vscode_keepalive_on_http`。同时记录发送方回环标记、临时源端口、当前请求是否是视频以及响应是否已开始/完成。保留 `unrecognized_protocol` 处理不匹配的数据；不会通过丢弃非法字节隐藏错误。

不记录原始请求头、Cookie、完整路径/查询参数或视频载荷。诊断文件有大小和轮转限制。

## 独立 SSH 对照

在**标注者电脑的终端**执行，SSH 目标替换为平时登录服务器时使用的主机别名或 `用户名@服务器地址`；如果原连接需指定端口或跳板，沿用相同 SSH 配置：

```bash
ssh -N -o ExitOnForwardFailure=yes -L 18001:127.0.0.1:8001 SSH目标
```

保留该终端运行，用 Chrome 打开 `http://localhost:18001`，登录并播放同一子任务至少 30 秒。新端口避开现有 VS Code 8001 转发。此命令应在电脑执行，在服务器内部执行不能绕过电脑侧 VS Code 转发。

若独立 SSH 正常而 VS Code 转发失败，应将日常访问切到独立 SSH 隧道，并处理本地 VS Code/Remote-SSH 转发问题。此时不能仅以服务器上的本机 Chrome 正常来宣称实际链路已修复。若两条路径均失败，继续对照增强诊断和浏览器 `ssi-video-error`，不要认定只是带宽不足。

现场证据与测试记录见 `benchmarks/http-forwarding-diagnosis-20261021.json`。
