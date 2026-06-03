// =====================================================================
//  Nano 继电器控制器  v1.1
//  适用:OpenCV 项目 Pi → USB 串口 → Arduino Nano → 5V 继电器 → 外设
//
//  串口协议(以 \n 或 \r 结尾的行):
//    Pi → Nano:
//      "PIN <n> <HIGH|LOW> <ms>"   在数字脚 <n> 拉到指定电平,持续 <ms> 毫秒后自动释放
//                                  非阻塞,多通道可并行,新命令会覆盖同脚的旧脉冲
//      "STOP [n]"                  立即停掉所有脉冲;带 n 则只停该脚
//      "STATUS"                    打印 D2..D12 当前电平(0/1)
//      "PING"                      心跳测试,回 PONG
//      "VER"                       回固件版本
//      "HELP"                      回命令清单
//    Nano → Pi:
//      "NANO_RELAY 1.1"            上电版本
//      "READY"                     上电就绪
//      "OK" / "OK STOP" / "OK STOP <n>"   命令接受
//      "PONG" / "STATUS D2=1 ..." / "HELP ..."
//      "ERR FORMAT|PIN|MS|LEVEL|UNKNOWN"  错误
//
//  v1.1 相对 v1.0 的改动:
//    - 脉冲从 delay() 阻塞改为 millis() 调度,可多通道并行
//    - 板上 LED(D13)1Hz 心跳,一眼看出 Nano 活着
//    - 加 PING/VER/STATUS/STOP/HELP 调试命令
//    - 错误码细分
//    - 上电主动发版本 + READY,方便 Pi 端握手
//
//  极性配置(改继电器模块极性时,只动下面 ACTIVE_LOW 一行):
//    ACTIVE_LOW = true  → 多数 5V 继电器模块(Nano 拉 LOW = 吸合),本项目默认
//    ACTIVE_LOW = false → 少数 active_high 模块
// =====================================================================

const long BAUD = 9600;
const uint8_t MIN_PIN = 2;
const uint8_t MAX_PIN = 12;
const unsigned long MAX_MS = 60000UL;     // 单次脉冲上限 60s
const unsigned long HEARTBEAT_MS = 500;   // LED 心跳周期

const bool ACTIVE_LOW = true;
const int IDLE_LEVEL = ACTIVE_LOW ? HIGH : LOW;

// 每脚一个脉冲记录(index 0..12,只用 2..12)
struct Pulse {
  bool active = false;
  unsigned long endMs = 0;
};
Pulse pulses[13];

String buf;
const char* FW_VER = "NANO_RELAY 1.1";

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  for (uint8_t p = MIN_PIN; p <= MAX_PIN; p++) {
    pinMode(p, OUTPUT);
    digitalWrite(p, IDLE_LEVEL);
  }

  Serial.begin(BAUD);
  delay(200);  // 给 Pi 端 pyserial 的上电复位窗口留时间
  Serial.println(FW_VER);
  Serial.println(F("READY"));
}

void loop() {
  // 1) 读串口 → 解析一行命令
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (buf.length() > 0) {
        handle(buf);
        buf = "";
      }
    } else {
      buf += c;
      if (buf.length() > 96) buf = "";  // 异常长输入直接清空,防溢出
    }
  }

  // 2) 处理到期的脉冲(非阻塞,所有脚并行)
  unsigned long now = millis();
  for (uint8_t p = MIN_PIN; p <= MAX_PIN; p++) {
    if (pulses[p].active && (long)(now - pulses[p].endMs) >= 0) {
      digitalWrite(p, IDLE_LEVEL);
      pulses[p].active = false;
    }
  }

  // 3) 板上 LED 心跳:1Hz 翻转,脉冲进行中快闪(2Hz)
  static unsigned long lastBlink = 0;
  static bool ledOn = false;
  bool anyActive = false;
  for (uint8_t p = MIN_PIN; p <= MAX_PIN; p++) {
    if (pulses[p].active) { anyActive = true; break; }
  }
  unsigned long interval = anyActive ? 150UL : HEARTBEAT_MS;
  if (now - lastBlink >= interval) {
    lastBlink = now;
    ledOn = !ledOn;
    digitalWrite(LED_BUILTIN, ledOn ? HIGH : LOW);
  }
}

// ---------------------------------------------------------------------
// 命令分发
// ---------------------------------------------------------------------
void handle(String line) {
  line.trim();

  if (line.startsWith("PIN "))        { handlePin(line);    return; }
  if (line.startsWith("STOP"))        { handleStop(line);   return; }
  if (line == "STATUS")               { handleStatus();     return; }
  if (line == "PING")                 { Serial.println(F("PONG")); return; }
  if (line == "VER")                  { Serial.println(FW_VER);     return; }
  if (line == "HELP")                 { handleHelp();       return; }

  Serial.println(F("ERR UNKNOWN"));
}

void handlePin(const String& line) {
  // 形如 "PIN 3 LOW 8000"
  int s1 = line.indexOf(' ', 4);
  int s2 = (s1 < 0) ? -1 : line.indexOf(' ', s1 + 1);
  if (s1 < 0 || s2 < 0) { Serial.println(F("ERR FORMAT")); return; }

  int pin = line.substring(4, s1).toInt();
  String level = line.substring(s1 + 1, s2);
  level.trim();
  long ms = line.substring(s2 + 1).toInt();

  if (pin < MIN_PIN || pin > MAX_PIN) { Serial.println(F("ERR PIN")); return; }
  if (level != "HIGH" && level != "LOW") { Serial.println(F("ERR LEVEL")); return; }
  if (ms <= 0) { Serial.println(F("ERR MS")); return; }
  if (ms > (long)MAX_MS) ms = (long)MAX_MS;

  int levelVal = (level == "HIGH") ? HIGH : LOW;
  triggerPulse((uint8_t)pin, levelVal, (unsigned long)ms);
  Serial.println(F("OK"));
}

void handleStop(const String& line) {
  if (line.length() == 4) {
    stopAll();
    Serial.println(F("OK STOP ALL"));
    return;
  }
  if (line[4] != ' ') { Serial.println(F("ERR FORMAT")); return; }
  int pin = line.substring(5).toInt();
  if (pin < MIN_PIN || pin > MAX_PIN) { Serial.println(F("ERR PIN")); return; }
  stopPin((uint8_t)pin);
  Serial.print(F("OK STOP "));
  Serial.println(pin);
}

void handleStatus() {
  Serial.print(F("STATUS"));
  for (uint8_t p = MIN_PIN; p <= MAX_PIN; p++) {
    Serial.print(' ');
    Serial.print('D'); Serial.print(p);
    Serial.print('=');
    Serial.print(digitalRead(p));
  }
  Serial.println();
}

void handleHelp() {
  Serial.println(F("HELP NANO_RELAY 1.1"));
  Serial.println(F("  PIN <n> <HIGH|LOW> <ms>   pulse pin n for <ms> ms"));
  Serial.println(F("  STOP [n]                  stop all pulses, or pin n"));
  Serial.println(F("  STATUS                    show D2..D12 levels (0/1)"));
  Serial.println(F("  PING                      reply PONG"));
  Serial.println(F("  VER                       firmware version"));
  Serial.println(F("  HELP                      this message"));
}

// ---------------------------------------------------------------------
// 脉冲底层
// ---------------------------------------------------------------------
void triggerPulse(uint8_t pin, int level, unsigned long ms) {
  digitalWrite(pin, level);
  pulses[pin].active = true;
  pulses[pin].endMs = millis() + ms;
}

void stopPin(uint8_t pin) {
  digitalWrite(pin, IDLE_LEVEL);
  pulses[pin].active = false;
}

void stopAll() {
  for (uint8_t p = MIN_PIN; p <= MAX_PIN; p++) {
    digitalWrite(p, IDLE_LEVEL);
    pulses[p].active = false;
  }
}
