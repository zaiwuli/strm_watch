# STRM Watch - 群晖 Docker 使用指南

## 📋 项目说明

**STRM Watch** 是一个为群晖 NAS 优化的监控工具，用于：
- 📁 实时监控源文件夹中的 `.strm` 文件
- 🔄 自动将文件中的旧路径前缀替换为新挂载路径
- 📤 将替换后的文件生成到目标文件夹
- 📨 可选通知：通过 MediaServer API 发送实时同步通知

## 🚀 快速开始

### 1. 群晖 Docker 中部署

**方法 A：使用 docker-compose（推荐）**

```bash
# 在 SSH 中执行
cd /volume1/docker/strm_watch
docker-compose up -d
```

**方法 B：使用 docker 命令**

```bash
docker run -d \
  --name strm_watch \
  --restart unless-stopped \
  -v /volume1/source:/source \
  -v /volume1/target:/target \
  -e SOURCE_DIR=/source \
  -e TARGET_DIR=/target \
  -e OLD_KEYWORD="/CloudNAS/CloudDrive/115open/" \
  -e NEW_MOUNT_PREFIX="/115/" \
  strm_watch:latest
```

**方法 C：群晖 Docker UI 中使用**

1. 在镜像中构建此项目
2. 新建容器时配置：
   - **端口**：无需配置
   - **卷**：
     - 源文件夹 → `/source`
     - 目标文件夹 → `/target`
   - **环境变量**：按下方配置

### 2. 环境变量配置

| 变量名 | 说明 | 示例 | 默认值 |
|--------|------|------|--------|
| `SOURCE_DIR` | 源文件夹路径（容器内） | `/source` | `/source` |
| `TARGET_DIR` | 目标文件夹路径（容器内） | `/target` | `/target` |
| `OLD_KEYWORD` | 需要替换的旧路径前缀 | `/CloudNAS/CloudDrive/115open/` | `/CloudNAS/CloudDrive/115open/` |
| `NEW_MOUNT_PREFIX` | 替换为的新路径前缀 | `/115/` | `/115/` |
| `MS_URL` | MediaServer 地址（可选） | `http://192.168.1.100:7001` | 空 |
| `MS_API_KEY` | MediaServer API Key（可选） | `your_api_key` | 空 |
| `ENABLE_URL_ENCODE` | 是否启用 URL 编码 | `True` / `False` | `True` |
| `TZ` | 时区 | `Asia/Shanghai` | `Asia/Shanghai` |

### 3. 工作原理

```
监控源文件夹 (/source)
    ↓
    检测到 .strm 文件创建/修改
    ↓
    读取文件内容
    ↓
    查找并替换 OLD_KEYWORD 为 NEW_MOUNT_PREFIX
    ↓
    应用 URL 编码（可选）
    ↓
    生成 MediaServer STRM302 API 链接
    ↓
    写入到目标文件夹 (/target)
    ↓
    发送通知（如已配置）
```

## 📊 日志和调试

### 查看容器日志

**群晖 UI 查看：**
- Docker → 容器 → strm_watch → 详情 → 日志

**SSH 命令查看：**
```bash
docker logs -f strm_watch
```

### 日志文件位置

日志同时写入：
- 标准输出（Docker 日志）
- 容器内 `/app/strm_watch.log`

在 docker-compose.yml 中可挂载日志卷：
```yaml
volumes:
  - /volume1/@appstore/strm_watch/logs:/app
```

### 常见问题排查

| 问题 | 解决方案 |
|------|--------|
| 权限错误 | 确保源/目标文件夹权限正确，容器有读写权限 |
| 文件未被处理 | 检查日志，确认是否包含 OLD_KEYWORD；检查文件格式 |
| 通知未发送 | MS_URL 和 MS_API_KEY 是否正确配置 |
| 时间戳不对 | 检查 TZ 环境变量和系统时区设置 |

## 🔧 常见配置场景

### 场景 1：115 网盘文件替换

```yaml
environment:
  OLD_KEYWORD: /CloudNAS/CloudDrive/115open/
  NEW_MOUNT_PREFIX: /115/
  ENABLE_URL_ENCODE: "True"
```

### 场景 2：其他存储路径替换

```yaml
environment:
  OLD_KEYWORD: /mnt/oldpath/
  NEW_MOUNT_PREFIX: /newpath/
```

### 场景 3：启用通知功能

```yaml
environment:
  MS_URL: http://192.168.1.100:7001
  MS_API_KEY: your-ms-api-key
```

## 📈 改进内容

✅ **增强的错误处理**
- 具体的错误日志，便于调试
- 权限错误单独处理
- 异常重试机制

✅ **完善的日志系统**
- 同时输出到控制台和文件
- 清晰的时间戳和错误级别
- 支持群晖日志轮转

✅ **群晖 NAS 优化**
- 资源限制防止过载
- 健康检查确保服务运行
- UTF-8 编码支持中文路径
- 时区自动配置

✅ **可靠性提升**
- 初始扫描统计报告
- 配置验证
- 重试机制（通知发送）
- 异常处理完善

✅ **容器配置完善**
- 缓冲输出关闭，实时显示日志
- 日志驱动和轮转策略
- 资源和健康检查
- 重启策略

## 🛑 停止和重启

```bash
# 查看容器
docker ps | grep strm_watch

# 停止容器
docker stop strm_watch

# 重启容器
docker restart strm_watch

# 删除容器
docker rm strm_watch
```

## 📝 其他说明

- 首次启动会进行初始扫描，处理源文件夹中所有符合条件的文件
- 监控是递归的，包含所有子文件夹
- 文件处理是异步的，不会阻塞监控
- 建议定期检查日志确保服务运行正常
