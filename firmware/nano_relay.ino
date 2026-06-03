// Nano 继电器控制器
// 协议: "PIN <脚号> <HIGH|LOW> <持续毫秒>\n"
// 示例: "PIN 3 LOW 8000"  → D3 拉低 8000ms 后回 HIGH (active_low 继电器开 8s)
// 回执: "OK" / "ERR ..."

const long BAUD = 9600;
const uint8_t MIN_PIN = 2;
const uint8_t MAX_PIN = 12;
const unsigned long MAX_MS = 60000UL;

// active_low 继电器模块的"释放"电平 = HIGH;改成 LOW 时才吸合
const int IDLE_LEVEL = HIGH;

String buf;

void setup() {
  Serial.begin(BAUD);
  for (uint8_t p = MIN_PIN; p <= MAX_PIN; p++) {
    pinMode(p, OUTPUT);
    digitalWrite(p, IDLE_LEVEL);   // 上电默认全部不吸合
  }
  Serial.println("READY");
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (buf.length() > 0) {
        handle(buf);
        buf = "";
      }
    } else {
      buf += c;
      if (buf.length() > 64) buf = "";  // 防溢出
    }
  }
}

void handle(const String& line) {
  if (!line.startsWith("PIN ")) {
    Serial.println("ERR unknown");
    return;
  }
  int s1 = line.indexOf(' ', 4);
  int s2 = line.indexOf(' ', s1 + 1);
  if (s1 < 0 || s2 < 0) {
    Serial.println("ERR format");
    return;
  }
  int pin = line.substring(4, s1).toInt();
  String level = line.substring(s1 + 1, s2);
  unsigned long ms = (unsigned long)line.substring(s2 + 1).toInt();

  if (pin < MIN_PIN || pin > MAX_PIN) { Serial.println("ERR pin"); return; }
  if (ms > MAX_MS) ms = MAX_MS;

  int active = (level == "HIGH") ? HIGH : LOW;
  int idle   = (level == "HIGH") ? LOW  : HIGH;

  digitalWrite(pin, active);
  delay(ms);
  digitalWrite(pin, idle);

  Serial.println("OK");
}
