# Nano 固件烧录 & 联调说明

> **固件文件**: `nano_relay.ino`(本目录)
> **协议**: `PIN <n> <HIGH|LOW> <ms>\n` —— 与 `devices/arduino.py` 里的 ArduinoDevice 完全对齐
> **波特率**: 9600
> **板子**: Arduino Nano(ATmega328P,旧版/新版都可以)

---

## 0. 整体逻辑

```
Pi(OpenCV + Flask)
  │  USB 串口
  ▼
Arduino Nano
  │  数字脚 D3 / D4(短脉冲)
  ▼
5V 继电器模块
  │  COM-NO 开关
  ▼
外设(按摩仪 / 电磁锁)
```

整个系统是 **Pi 算 → 发状态 → Nano 拉脚 → 继电器开关外设**。
固件只负责"收到命令后,拉某个脚 N 毫秒"——纯文本协议,出问题时肉眼可读。

---

## 1. 准备

### 1.1 接线(还没接的看这里)

| Nano 脚 | 接到 | 说明 |
|---|---|---|
| USB | Pi 的 USB 口 | 供电 + 串口,数据线要能传数据(不能是纯充电线) |
| D3 | 继电器 CH1 的 IN | 触发后吸合 8s(按摩仪) |
| D4 | 继电器 CH2 的 IN | 触发后吸合 2s(电磁锁) |
| GND | 继电器 GND | **必须共地** |
| 5V | 继电器 VCC | USB 自带 5V,够继电器逻辑端 |

> 详细接线图和电源注意事项看 `DEVICES.md` 的"接线表"小节。

### 1.2 装 Arduino IDE

1. 打开 https://www.arduino.cc/en/software
2. 下载 **Windows Installer**(或你系统对应的版本)
3. 装好,首次启动会让你装 USB 驱动——一路 Next
4. 国产 Nano 用的是 **CH340** 芯片,Win10/11 通常免驱自动装;如果看不到串口,手动装:https://www.wch-ic.com/downloads/CH341SER_EXE.html

### 1.3 插上 Nano

- 把 Nano 用 USB 数据线接到电脑
- Win 系统应该听到"叮"一声 + 设备管理器里看到"USB-SERIAL CH340 (COMx)"
- **记下这个 COM 端口号**(比如 COM3、COM7),后面要用

---

## 2. 烧录

### 2.1 打开工程

1. 启动 Arduino IDE
2. `File → Open...`,选 `nano_relay.ino`
3. `Tools → Board → Arduino AVR Boards → Arduino Nano`
4. `Tools → Processor → ATmega328P`(旧版)或 `ATmega328P (Old Bootloader)`(如果上传失败试这个)
5. `Tools → Port → COMx`(选刚才记下的端口)

### 2.2 上传

1. 点左上角 `→ Upload`(快捷键 Ctrl+U)
2. 第一次会上传稍慢(IDE 内部编译),底部日志里会滚
3. 出现 `Done uploading.` 就是成功了
4. **如果报错**:`avrdude: stk500_getsync() ... not in sync`,把 `Processor` 切成 `Old Bootloader` 再试

### 2.3 烧录成功的标志

- Nano 上的 **L 灯(D13 心跳)** 开始每 500ms 闪一次
- 插着 USB 的话,电脑设备管理器里 COMx 还在

---

## 3. 串口监视器自测

不接 Pi,直接用 IDE 自带的串口监视器手测固件逻辑。

### 3.1 打开串口监视器

1. `Tools → Serial Monitor`(快捷键 Ctrl+Shift+M)
2. 右下角选 **9600 baud**(必须和固件一致,默认 9600 不用改)
3. 左下角选 **Newline**(发送自动加 `\n`)

打开后应该立刻看到两行:

```
NANO_RELAY 1.1
READY
```

看到这两行 → 固件跑起来了。如果只看到乱码,说明波特率没对齐(IDE 必须是 9600)。

### 3.2 跑一遍调试命令

在串口监视器顶部输入框敲命令,点 Send:

| 你输入 | 你应该看到 | 说明 |
|---|---|---|
| `PING` | `PONG` | 心跳 OK |
| `VER` | `NANO_RELAY 1.1` | 版本号 |
| `HELP` | 6 行帮助文字 | 命令清单 |
| `STATUS` | `STATUS D2=1 D3=1 D4=1 ... D12=1` | 11 个脚的当前电平,全 1 = 都处于 IDLE(继电器释放) |
| `PIN 13 LOW 1000` | `ERR PIN` | 试一下边界检查:D13 不在合法范围(2~12) |
| `PIN 3 LOW 1000` | `OK` | 触发 D3 拉低 1 秒,继电器应该响一下 |

> **看 D3 的反应**:
> - 在 `STATUS` 里,D3 应该先变 `0`(吸合中),1 秒后变回 `1`(释放)
> - 板上 L 灯在脉冲期间会**快闪**(2Hz 间隔 150ms),脉冲结束后恢复慢闪(1Hz 间隔 500ms)
> - 如果继电器模块上一般也有 LED,D3 触发时它会亮

### 3.3 联调 Pi 端(如果 Pi 还没上,先跳过这节)

把 Nano 从电脑拔下来,接到树莓派:

```bash
# Pi 端
ls /dev/ttyUSB* /dev/ttyACM*    # 应该看到 ttyUSB0(或 ACM0)
# 没看到 → 检查数据线 / 重新插拔
# 权限不足 → sudo usermod -aG dialout $USER,重新登录

# 启动 web 服务
cd ~/OpenCV
source .venv/bin/activate
python web_server.py --host 0.0.0.0 --port 8000 --mode closed_eye --source 0
```

启动日志应该看到:
```
[arduino:massager_power] /dev/ttyUSB0@9600 ready, pin=3
[arduino:door_lock] /dev/ttyUSB0@9600 ready, pin=4
路由注册: closed_eye.shock → <ArduinoDevice massager_power> (cooldown=60.0s)
路由注册: pushup.unlock → <ArduinoDevice door_lock> (cooldown=30.0s)
```

> 看到 `ready, pin=3` 说明握手成功。如果看到 `串口不可用,降级 dummy`,检查 USB 线和 dialout 组。

打开浏览器 `http://<Pi IP>:8000`,进入闭眼检测模式,闭眼超过阈值后会触发按摩仪,D3 拉低 8 秒。

### 3.4 手动从 Pi 端发命令测试

如果想跳过 OpenCV,直接用 `minicom`/`picocom` 从 Pi 端发命令测固件:

```bash
# 装一个串口工具
sudo apt install -y minicom

# 接 Nano(按 Ctrl+A 再按 X 退出)
minicom -b 9600 -o -D /dev/ttyUSB0
```

进 minicom 后手动输入:
```
PING
VER
STATUS
PIN 3 LOW 3000
```

Pi 端和电脑 IDE 端协议完全一样,看到的回显也完全一样。

---

## 4. 故障排查

| 现象 | 排查 |
|---|---|
| IDE 上传报错 `not in sync` | 切到 `Old Bootloader`;或换 USB 数据线(很多廉价线只能充电) |
| 设备管理器看不到 COM 端口 | Win:装 CH340 驱动;Linux:`dmesg \| tail` 看插入日志 |
| 串口监视器看不到 `READY` | 波特率没对齐(IDE 必须是 9600);或按住 Nano 的 RESET 按钮后再打开监视器 |
| 看到 `READY` 但发 `PING` 没回 `PONG` | 监视器右下角没切到 "Newline";或发送框里多了空格 |
| `PIN 3 LOW 1000` 没反应 | 继电器模块 VCC 没接 / 极性反(改固件顶部 `ACTIVE_LOW`);或负载本身没通电 |
| 触发瞬间 Pi 重启 | 5V 电源带不动继电器,继电器 VCC 接独立 5V,GND 共地(见 `DEVICES.md`) |
| Pi 端日志 `串口不可用,降级 dummy` | `sudo usermod -aG dialout $USER` + 重新登录;或 USB 没插紧 |

---

## 5. 想换继电器极性 / 改引脚?

只需要改 `nano_relay.ino` 顶部几行,重新烧录即可:

```cpp
const bool ACTIVE_LOW = true;       // 改 true/false
const uint8_t MIN_PIN = 2;          // 改允许的最小脚号
const uint8_t MAX_PIN = 12;         // 改允许的最大脚号
const unsigned long MAX_MS = 60000; // 改脉冲上限
```

Python 端 `config/devices.yaml` 里 `active_high` 字段也要同步(取反):

```yaml
# 固件 ACTIVE_LOW=true(默认)  →  yaml active_high=false
# 固件 ACTIVE_LOW=false        →  yaml active_high=true
```

---

## 6. 协议速查(给写代码的人)

```
发送: PIN 3 LOW 8000\n
回执: OK\n        (立即回,不阻塞)

发送: STOP\n
回执: OK STOP ALL\n

发送: STATUS\n
回执: STATUS D2=1 D3=0 D4=1 D5=1 D6=1 D7=1 D8=1 D9=1 D10=1 D11=1 D12=1\n

发送: PING\n   →  PONG\n
发送: VER\n    →  NANO_RELAY 1.1\n
发送: HELP\n   →  6 行帮助
```

错误一律 `ERR <CODE>`,CODE ∈ {FORMAT, PIN, MS, LEVEL, UNKNOWN}。
