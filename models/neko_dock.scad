// Нэко — скелет для 3D-печати: док-станция + колонка (короткий цилиндр).
// Параметрический. Подгоняй константы под свои платы.
//
// part = "speaker" | "dock" | "both"
//   openscad -D 'part="dock"' -o dock.stl FILE.scad
$fn = 120;

part = "both";          // speaker | dock | both

// ============================ КОЛОНКА ============================
SPK_D     = 92;         // внешний диаметр цилиндра
H_SPK     = 125;        // высота (короткий цилиндр)
WALL      = 3;          // толщина стенки
BASE_T    = 8;          // толщина дна (туда уходит бобышка дока)
TOP_T     = 4;          // толщина крышки

// OLED 0.96" SSD1306 (модуль ~27x27 мм)
OL_W      = 28;         // окно под экран
OL_H      = 28;
OL_R      = 4;          // скругление окна
OL_Y      = H_SPK - 52; // центр окна от низа

FLAT_DEPTH = 9;         // насколько срезаем "лоб" цилиндра под экран/кнопки
FLAT_W     = 52;        // ширина плоского участка

// Кнопки (3 шт: VOL+, MAIN, VOL-)
BTN_D     = 7;          // диаметр кнопочного отверстия
BTN_YS    = [58, 74, 90];

// Гриль вокруг низа
GRILL_H   = 26;
GRILL_W   = 4;
GRILL_N   = 20;

// Микрофон INMP441 — отверстия сверху кластером
MIC_R     = 6;
MIC_D     = 3;
MIC_N     = 5;

// ============================= ДОК =============================
GAP       = 2;          // зазор посадки колонки в док
WALL_DOCK = 3.5;
FLOOR_T   = 4;          // толщина пола дока
H_DOCK    = 30;         // высота дока
BOSS_D    = 40;         // диаметр центральной бобышки (входит в днище колонки)
BOSS_H    = 14;         // высота бобышки
CONN_W    = 14;         // окно коннектора (ширина)
CONN_H    = 8;          // окно коннектора (высота)
CABLE_W   = 12;         // канал кабеля (ширина)
CABLE_H   = 10;         // канал кабеля (высота)

face_x = SPK_D / 2 - FLAT_DEPTH;        // координата плоского "лба"
spk_in = SPK_D - 2 * WALL;              // внутренний диаметр трубки

// ------------------------- ВСПОМОГАТЕЛЬНОЕ -------------------------
// округлённый прямоугольник-«прут» вдоль оси X (толщина t)
module rod_round(w, h, r, t) {
  hull() {
    for (sy = [r, w - r])
      for (sz = [r, h - r])
        translate([0, sy - w / 2, sz - h / 2])
          rotate([0, -90, 0]) cylinder(r = r, h = t, center = true);
  }
}

// ------------------------------ КОЛОНКА ------------------------------
// Единый твёрдый объект: трубка + дно + крышка, вся геометрия вырезается
// (никаких "плавающих" объёмов, поэтому на выходе 1 solid).
module speaker() {
  difference() {
    // тело колонки
    cylinder(d = SPK_D, h = H_SPK);

    // внутренняя полость (дно BASE_T, крышка TOP_T)
    translate([0, 0, BASE_T])
      cylinder(d = spk_in, h = H_SPK - BASE_T - TOP_T + 0.01);

    // лоб под экран и кнопки (плоский участок срезается с цилиндра)
    translate([face_x - FLAT_DEPTH - 1, -FLAT_W / 2, -1])
      cube([FLAT_DEPTH + 1, FLAT_W, H_SPK + 2]);

    // окно OLED
    translate([face_x - WALL - 1.5, 0, OL_Y])
      rod_round(OL_W, OL_H, OL_R, WALL + 3);

    // кнопки
    for (y = BTN_YS)
      translate([face_x - 1, 0, y])
        rotate([0, -90, 0]) cylinder(d = BTN_D, h = WALL + 2, center = true);

    // гриль — вертикальные щели по окружности низа, сквозь стенку
    // (от края полости до внешней поверхности)
    for (i = [0:GRILL_N - 1])
      rotate([0, 0, i * 360 / GRILL_N])
        translate([SPK_D / 2 - WALL - 0.3, -GRILL_W / 2, BASE_T - 1])
          cube([WALL + 1.5, GRILL_W, GRILL_H]);

    // отверстия микрофона в крышке
    for (i = [0:MIC_N - 1]) {
      a = i * 360 / MIC_N;
      translate([MIC_R * cos(a), MIC_R * sin(a), H_SPK - 0.01])
        cylinder(d = MIC_D, h = TOP_T + 1);
    }
    translate([0, 0, H_SPK - 0.01]) cylinder(d = MIC_D, h = TOP_T + 1);

    // посадочное углубление под бобышку дока (со стороны днища)
    translate([0, 0, -0.01])
      cylinder(d = BOSS_D + 1, h = BOSS_H + 0.5);
  }
}

// ------------------------------ ДОК ------------------------------
// Один твёрдый объект: кубок с полом + бобышка, всё перекрывается.
module dock() {
  od = SPK_D + 2 * GAP + 2 * WALL_DOCK;   // наружный диаметр дока
  id = SPK_D + 2 * GAP;                   // внутренний диаметр кубка (посадочный)
  difference() {
    union() {
      // кубок (стенка + пол+бобышка одним телом)
      cylinder(d = od, h = H_DOCK);
    }

    // выемка для колонки
    translate([0, 0, FLOOR_T])
      cylinder(d = id, h = H_DOCK - FLOOR_T + 0.01);

    // канал кабеля: от центра наружу (сквозь пол и стенку)
    translate([0, -CABLE_W / 2, FLOOR_T / 2 - 0.01])
      cube([od / 2 + 1, CABLE_W, CABLE_H]);

    // паз под коннектор в стене дока справа от кабельного канала
    translate([0, -CONN_W / 2, FLOOR_T + BOSS_H - CONN_H])
      cube([od / 2 - WALL_DOCK + 0.5, CONN_W, CONN_H]);
  }

  // бобышка под коннектор — перекрывается с полом (срастётся в один solid)
  translate([0, 0, FLOOR_T - 0.01]) cylinder(d = BOSS_D, h = BOSS_H);
}

// ------------------------------ СБОРКА ------------------------------
color("Gainsboro") if (part == "speaker" || part == "both") speaker();
color("Tan") if (part == "dock" || part == "both")
  translate([0, 0, -H_DOCK]) dock();