/* ATLAS-DIAG application layer for NUCLEO-F446RE (project "Atlas").
 *
 * Ported from the earlier F446RE build that used a 16x2 LCD (2026-09-16). Changes:
 *   - 16x2 LCD calls replaced by the SSD1306 0.96" OLED (I2C1, 0x3C).
 *   - LD1/LD3 replaced by LED_PASS (PB12) / LED_FAULT (PA8, D7).
 *   - Idle / 'X' / teardown pins use GPIO INPUT, not ANALOG: FT (5 V tolerant)
 *     does not apply in analog mode (DS10693), and DUT outputs are ~5 V.
 *   - B1 on Nucleo-64 is active LOW (pressed = RESET); the old code assumed SET.
 *
 * UART protocol (unchanged, matches software/pc_tester.py):
 *   PING -> PONG | SEL:<0-5> -> SEL_OK | CFG:<14 chars I/O/X> -> CFG_OK
 *   V:<one bit per 'I' pin> -> R:<one bit per 'O' pin>
 *   RES:PASS | RES:FAULT:<txt> | RES:UNKNOWN:<sig> | RES:VERIFY_FAIL:<n/N> -> RES_OK
 *   errors -> ERR:...
 */
#include "main.h"
#include "atlas_app.h"
#include "ssd1306.h"
#include "ssd1306_fonts.h"
#include "ic_table_common.h"
#include "ic_table_74HCT00.h"
#include "ic_table_74HCT02.h"
#include "ic_table_74HCT04.h"
#include "ic_table_74HCT08.h"
#include "ic_table_74HCT32.h"
#include "ic_table_74HCT86.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>

extern UART_HandleTypeDef huart2;
extern I2C_HandleTypeDef hi2c1;

/* CubeMX may name the fault LED LED_FAULT or LED_FAULT1 -- accept either. */
#if !defined(LED_FAULT_Pin) && defined(LED_FAULT1_Pin)
#define LED_FAULT_Pin       LED_FAULT1_Pin
#define LED_FAULT_GPIO_Port LED_FAULT1_GPIO_Port
#endif

#define RX_BUFFER_SIZE        64
#define NUM_DUT_PINS          14
#define NUM_SUPPORTED_ICS     6
#define SIGNATURE_BUFFER_SIZE 64
#define MAX_ALIASED_CANDIDATES 8
#define STANDALONE_CYCLE_MS   1500
#define LONG_PRESS_MS         1000

/* External test button: Arduino D2 = PA10 (5 V tolerant, otherwise unused).
 * Wire: D2 -> push button -> GND. Internal pull-up, so no resistor needed.
 * Works exactly like B1 (tap = next IC, hold > 1 s = run test); B1 still works too. */
#define EXT_BTN_GPIO_Port     GPIOA
#define EXT_BTN_Pin           GPIO_PIN_10

/* 'I' = STM32 output (DUT input), 'O' = STM32 input (DUT output), 'X' = unused */
static char pin_config[NUM_DUT_PINS];

typedef struct { GPIO_TypeDef *port; uint16_t pin; } DutPin;

/* Index i = DIP pin (i+1). Matches the breadboard wiring (1 kOhm series each).
 * DIP 7 (PC8) and DIP 14 (PB2) are NOT wired -- DIP 7/14 go to the GND/5 V rails.
 * Every IC's pin_config has 'X' at those two positions, so they are never driven. */
static const DutPin pin_map[NUM_DUT_PINS] = {
    {GPIOC, GPIO_PIN_2},  {GPIOC, GPIO_PIN_3},  {GPIOC, GPIO_PIN_4},  {GPIOC, GPIO_PIN_5},
    {GPIOC, GPIO_PIN_6},  {GPIOC, GPIO_PIN_7},  {GPIOC, GPIO_PIN_8},  {GPIOC, GPIO_PIN_9},
    {GPIOC, GPIO_PIN_10}, {GPIOC, GPIO_PIN_11}, {GPIOC, GPIO_PIN_12}, {GPIOD, GPIO_PIN_2},
    {GPIOB, GPIO_PIN_1},  {GPIOB, GPIO_PIN_2},
};

typedef struct {
    const char *name;
    const char *pin_config;
    const char **vectors;
    uint8_t num_vectors;
    const IcFaultEntry *fault_table;
    uint16_t num_fault_entries;
    uint8_t num_inputs;              /* number of 'I' pins */
    uint8_t num_outputs;             /* number of 'O' pins */
    const unsigned char *golden;     /* 2^num_inputs fault-free responses */
    uint16_t num_golden;
} IcDescriptor;

static const IcDescriptor IC_REGISTRY[NUM_SUPPORTED_ICS] = {
    { IC_74HCT00_NAME, IC_74HCT00_PIN_CONFIG, IC_74HCT00_VECTORS, IC_74HCT00_NUM_VECTORS, IC_74HCT00_FAULT_TABLE, IC_74HCT00_NUM_FAULT_ENTRIES,
      IC_74HCT00_NUM_INPUTS, IC_74HCT00_NUM_OUTPUTS, IC_74HCT00_GOLDEN, IC_74HCT00_NUM_GOLDEN },
    { IC_74HCT02_NAME, IC_74HCT02_PIN_CONFIG, IC_74HCT02_VECTORS, IC_74HCT02_NUM_VECTORS, IC_74HCT02_FAULT_TABLE, IC_74HCT02_NUM_FAULT_ENTRIES,
      IC_74HCT02_NUM_INPUTS, IC_74HCT02_NUM_OUTPUTS, IC_74HCT02_GOLDEN, IC_74HCT02_NUM_GOLDEN },
    { IC_74HCT04_NAME, IC_74HCT04_PIN_CONFIG, IC_74HCT04_VECTORS, IC_74HCT04_NUM_VECTORS, IC_74HCT04_FAULT_TABLE, IC_74HCT04_NUM_FAULT_ENTRIES,
      IC_74HCT04_NUM_INPUTS, IC_74HCT04_NUM_OUTPUTS, IC_74HCT04_GOLDEN, IC_74HCT04_NUM_GOLDEN },
    { IC_74HCT08_NAME, IC_74HCT08_PIN_CONFIG, IC_74HCT08_VECTORS, IC_74HCT08_NUM_VECTORS, IC_74HCT08_FAULT_TABLE, IC_74HCT08_NUM_FAULT_ENTRIES,
      IC_74HCT08_NUM_INPUTS, IC_74HCT08_NUM_OUTPUTS, IC_74HCT08_GOLDEN, IC_74HCT08_NUM_GOLDEN },
    { IC_74HCT32_NAME, IC_74HCT32_PIN_CONFIG, IC_74HCT32_VECTORS, IC_74HCT32_NUM_VECTORS, IC_74HCT32_FAULT_TABLE, IC_74HCT32_NUM_FAULT_ENTRIES,
      IC_74HCT32_NUM_INPUTS, IC_74HCT32_NUM_OUTPUTS, IC_74HCT32_GOLDEN, IC_74HCT32_NUM_GOLDEN },
    { IC_74HCT86_NAME, IC_74HCT86_PIN_CONFIG, IC_74HCT86_VECTORS, IC_74HCT86_NUM_VECTORS, IC_74HCT86_FAULT_TABLE, IC_74HCT86_NUM_FAULT_ENTRIES,
      IC_74HCT86_NUM_INPUTS, IC_74HCT86_NUM_OUTPUTS, IC_74HCT86_GOLDEN, IC_74HCT86_NUM_GOLDEN },
};

typedef struct {
    uint8_t match_found;
    uint8_t is_fault;
    uint8_t num_candidates;
    const char *fault_names[MAX_ALIASED_CANDIDATES];
    const char *displays[MAX_ALIASED_CANDIDATES];
} DiagnosisResult;

typedef enum { DISPLAY_IDLE, DISPLAY_PASS, DISPLAY_FAULT_SINGLE, DISPLAY_FAULT_CYCLING, DISPLAY_UNKNOWN } DisplayState;

static char rx_buffer[RX_BUFFER_SIZE];
static uint8_t rx_index = 0;
static uint8_t selected_ic_index = 0;
static DisplayState display_state = DISPLAY_IDLE;
static DiagnosisResult last_result;
static uint8_t cycle_index = 0;
static uint32_t last_cycle_tick = 0;

/* ---------------------------------------------------------------- UART */
static void UartSend(const char *s)
{
    HAL_UART_Transmit(&huart2, (uint8_t *)s, (uint16_t)strlen(s), HAL_MAX_DELAY);
}

/* ---------------------------------------------------------------- I2C recovery
 * If the MCU resets in the middle of an OLED transfer, the SSD1306 can keep
 * holding SDA low and the STM32 I2C then reports BUSY forever (until a full
 * power cycle). Fix: clock SCL 9 times by hand, send a STOP, re-init I2C1. */
static void I2C_BusRecover(void)
{
    HAL_I2C_DeInit(&hi2c1);
    __HAL_RCC_I2C1_FORCE_RESET();
    __HAL_RCC_I2C1_RELEASE_RESET();

    GPIO_InitTypeDef g = {0};
    __HAL_RCC_GPIOB_CLK_ENABLE();
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8 | GPIO_PIN_9, GPIO_PIN_SET);
    g.Pin = GPIO_PIN_8 | GPIO_PIN_9;          /* PB8 = SCL, PB9 = SDA */
    g.Mode = GPIO_MODE_OUTPUT_OD;
    g.Pull = GPIO_PULLUP;
    g.Speed = GPIO_SPEED_FREQ_LOW;
    HAL_GPIO_Init(GPIOB, &g);

    for (int i = 0; i < 9; i++) {             /* 9 clocks release a stuck slave */
        HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8, GPIO_PIN_RESET); HAL_Delay(1);
        HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8, GPIO_PIN_SET);   HAL_Delay(1);
    }
    /* STOP: SDA low -> high while SCL high */
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_9, GPIO_PIN_RESET); HAL_Delay(1);
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8, GPIO_PIN_SET);   HAL_Delay(1);
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_9, GPIO_PIN_SET);   HAL_Delay(1);

    HAL_I2C_Init(&hi2c1);                     /* MspInit puts PB8/PB9 back to I2C AF */
}

static uint8_t OledPresent(void)
{
    return HAL_I2C_IsDeviceReady(&hi2c1, SSD1306_I2C_ADDR, 3, 20) == HAL_OK;
}

/* ---------------------------------------------------------------- OLED
 * 128x64. Layout: title (7x10) / IC line (7x10) / big status (11x18) / detail (7x10). */
static void OledText(uint8_t y, const char *s, SSD1306_Font_t font)
{
    char buf[20];
    snprintf(buf, sizeof(buf), "%-18s", s); /* pad with spaces so old text is erased */
    ssd1306_SetCursor(0, y);
    ssd1306_WriteString(buf, font, White);
}

static void OledShow(const char *ic_line, const char *status, const char *detail)
{
    /* Re-initialise the OLED before every full screen. If the 5 V rail dips
     * (e.g. an IC is inserted with power on) the SSD1306 resets itself to
     * "display off" and would stay blank until the next MCU reset. ~0.3 s. */
    if (!OledPresent()) I2C_BusRecover();
    ssd1306_Init();
    ssd1306_Fill(Black);
    OledText(0,  "ATLAS-DIAG", Font_7x10);
    OledText(14, ic_line, Font_7x10);
    OledText(28, status, Font_11x18);
    OledText(52, detail, Font_7x10);
    ssd1306_UpdateScreen();
}

static void OledDetail(const char *detail)
{
    OledText(52, detail, Font_7x10);
    ssd1306_UpdateScreen();
}

static void SetResultLeds(uint8_t pass, uint8_t fault)
{
    HAL_GPIO_WritePin(LED_PASS_GPIO_Port,  LED_PASS_Pin,  pass  ? GPIO_PIN_SET : GPIO_PIN_RESET);
    HAL_GPIO_WritePin(LED_FAULT_GPIO_Port, LED_FAULT_Pin, fault ? GPIO_PIN_SET : GPIO_PIN_RESET);
}

/* ---------------------------------------------------------------- DUT pins */
/* Park every DUT line as a plain input (high-Z, still 5 V tolerant) before
 * any reconfiguration, so no pin is ever driven against the DUT. */
static void TeardownAllPins(void)
{
    GPIO_InitTypeDef g = {0};
    g.Mode = GPIO_MODE_INPUT;
    g.Pull = GPIO_NOPULL;
    for (int i = 0; i < NUM_DUT_PINS; i++) {
        g.Pin = pin_map[i].pin;
        HAL_GPIO_Init(pin_map[i].port, &g);
    }
}

static void ConfigurePins(const char *cfg)
{
    TeardownAllPins();
    GPIO_InitTypeDef g = {0};

    for (int i = 0; i < NUM_DUT_PINS; i++) {
        pin_config[i] = cfg[i];
        g.Pin = pin_map[i].pin;

        if (cfg[i] == 'I') {            /* STM32 drives the DUT input */
            HAL_GPIO_WritePin(pin_map[i].port, pin_map[i].pin, GPIO_PIN_RESET);
            g.Mode = GPIO_MODE_OUTPUT_PP;
            g.Pull = GPIO_NOPULL;
            g.Speed = GPIO_SPEED_FREQ_LOW;
        } else if (cfg[i] == 'O') {     /* STM32 reads the DUT output */
            g.Mode = GPIO_MODE_INPUT;
            g.Pull = GPIO_PULLDOWN;     /* open/dead output reads 0, not random */
        } else {                        /* 'X' -- unused / power pin */
            g.Mode = GPIO_MODE_INPUT;
            g.Pull = GPIO_PULLDOWN;
        }
        HAL_GPIO_Init(pin_map[i].port, &g);
    }
}

static uint8_t CountPins(const char *cfg, char type)
{
    uint8_t n = 0;
    for (int i = 0; i < NUM_DUT_PINS; i++) if (cfg[i] == type) n++;
    return n;
}

/* Single source of truth for one electrical test step (PC and standalone). */
static void ApplyVectorAndRead(const char *vector, char *response)
{
    int v = 0;
    for (int i = 0; i < NUM_DUT_PINS; i++) {
        if (pin_config[i] == 'I') {
            if (vector[v] == '\0') break;
            HAL_GPIO_WritePin(pin_map[i].port, pin_map[i].pin,
                              (vector[v] == '1') ? GPIO_PIN_SET : GPIO_PIN_RESET);
            v++;
        }
    }

    HAL_Delay(1); /* propagation delay (HCT is ~ns; 1 ms is generous) */

    int r = 0;
    for (int i = 0; i < NUM_DUT_PINS; i++) {
        if (pin_config[i] == 'O') {
            response[r++] = (HAL_GPIO_ReadPin(pin_map[i].port, pin_map[i].pin) == GPIO_PIN_SET) ? '1' : '0';
        }
    }
    response[r] = '\0';
}

/* ---------------------------------------------------------------- PC protocol */
static void ProcessCommand(char *cmd)
{
    char line[RX_BUFFER_SIZE];

    if (strncmp(cmd, "PING", 4) == 0) {
        UartSend("PONG\n");
    }
    else if (strncmp(cmd, "SEL:", 4) == 0) {
        int idx = atoi(cmd + 4);
        if (idx >= 0 && idx < NUM_SUPPORTED_ICS) {
            selected_ic_index = (uint8_t)idx;
            snprintf(line, sizeof(line), "IC:%s", IC_REGISTRY[idx].name);
            /* Update the OLED BEFORE replying: the PC sends CFG: right after
             * SEL_OK, and the UART is polled, so we must not be busy then. */
            OledShow(line, "PC MODE", "Testing...");
            SetResultLeds(0, 0);
            display_state = DISPLAY_IDLE;
            UartSend("SEL_OK\n");
        } else {
            UartSend("ERR:SEL\n");
        }
    }
    else if (strncmp(cmd, "CFG:", 4) == 0) {
        if (strlen(cmd) >= 4 + NUM_DUT_PINS) {
            ConfigurePins(cmd + 4);
            UartSend("CFG_OK\n");
        } else {
            UartSend("ERR:LEN\n");
        }
    }
    else if (strncmp(cmd, "V:", 2) == 0) {
        const char *vector = cmd + 2;
        if (strlen(vector) != CountPins(pin_config, 'I')) {
            UartSend("ERR:VLEN\n");
        } else {
            char response[NUM_DUT_PINS + 1];
            HAL_GPIO_WritePin(LD2_GPIO_Port, LD2_Pin, GPIO_PIN_SET);   /* busy */
            ApplyVectorAndRead(vector, response);
            HAL_GPIO_WritePin(LD2_GPIO_Port, LD2_Pin, GPIO_PIN_RESET);
            snprintf(line, sizeof(line), "R:%s\n", response);
            UartSend(line);
        }
    }
    else if (strncmp(cmd, "RES:", 4) == 0) {
        /* Final diagnosis sent back by pc_tester.py so the board shows it too:
         *   RES:PASS | RES:FAULT:<gate text> | RES:UNKNOWN:<signature> | RES:VERIFY_FAIL:<n>/<N> */
        const char *arg = cmd + 4;
        char ic_line[24];
        snprintf(ic_line, sizeof(ic_line), "IC:%s", IC_REGISTRY[selected_ic_index].name);
        display_state = DISPLAY_IDLE;

        if (strncmp(arg, "PASS", 4) == 0) {
            OledShow(ic_line, "PASS", "PC verified");
            SetResultLeds(1, 0);
        } else if (strncmp(arg, "FAULT:", 6) == 0) {
            OledShow(ic_line, "FAULT", arg + 6);
            SetResultLeds(0, 1);
        } else if (strncmp(arg, "UNKNOWN:", 8) == 0) {
            OledShow(ic_line, "UNKNOWN", arg + 8);
            SetResultLeds(0, 1);
        } else if (strncmp(arg, "VERIFY_FAIL:", 12) == 0) {
            snprintf(line, sizeof(line), "Bad %s", arg + 12);
            OledShow(ic_line, "FAULT", line);
            SetResultLeds(0, 1);
        } else {
            UartSend("ERR:RES\n");
            return;
        }
        UartSend("RES_OK\n"); /* reply AFTER the OLED update (UART is polled) */
    }
    else {
        UartSend("ERR:CMD\n");
    }
}

static void PollUart(void)
{
    uint8_t ch;
    /* Drain every byte that is waiting; 1 ms timeout keeps the loop responsive. */
    while (HAL_UART_Receive(&huart2, &ch, 1, 1) == HAL_OK) {
        if (ch == '\n' || ch == '\r') {
            if (rx_index > 0) {
                rx_buffer[rx_index] = '\0';
                ProcessCommand(rx_buffer);
                rx_index = 0;
            }
        } else if (rx_index < RX_BUFFER_SIZE - 1) {
            rx_buffer[rx_index++] = (char)ch;
        } else {
            rx_index = 0; /* overflow -- drop the line */
        }
    }
}

/* ---------------------------------------------------------------- Standalone */
static void LookupSignature(const IcDescriptor *d, const char *sig, DiagnosisResult *res)
{
    memset(res, 0, sizeof(*res));
    for (uint16_t i = 0; i < d->num_fault_entries; i++) {
        if (strcmp(d->fault_table[i].signature, sig) == 0) {
            res->match_found = 1;
            if (res->num_candidates < MAX_ALIASED_CANDIDATES) {
                res->fault_names[res->num_candidates] = d->fault_table[i].fault_name;
                res->displays[res->num_candidates]    = d->fault_table[i].display_string;
                res->num_candidates++;
            }
        }
    }
    res->is_fault = !(res->match_found && res->num_candidates == 1 &&
                      strcmp(res->fault_names[0], "Good") == 0);
}

static void ShowSelectedIcPrompt(void)
{
    char line[24];
    snprintf(line, sizeof(line), "IC:%s", IC_REGISTRY[selected_ic_index].name);
    OledShow(line, "READY", "Tap=IC  Hold=Test");
    display_state = DISPLAY_IDLE;
}

/* Full (exhaustive) golden-run check, entirely on the board -- the same
 * test pc_tester.py --verify does. Applies all 2^n input vectors and compares
 * every response with the digital twin's fault-free table (IC_xx_GOLDEN).
 * Catches everything the 3-vector compacted set cannot: power/ground faults,
 * bridged pins, wrong chip, multiple faults. Returns number of mismatches. */
typedef struct {
    uint16_t total;
    uint16_t mismatches;
    uint16_t first_bad_index;      /* valid when mismatches > 0 */
    char first_bad_vector[NUM_DUT_PINS + 1];
    char first_bad_expected[NUM_DUT_PINS + 1];
    char first_bad_got[NUM_DUT_PINS + 1];
} FullCheckResult;

static void RunFullCheck(const IcDescriptor *d, FullCheckResult *fc)
{
    char vector[NUM_DUT_PINS + 1];
    char response[NUM_DUT_PINS + 1];
    memset(fc, 0, sizeof(*fc));
    fc->total = d->num_golden;

    for (uint16_t i = 0; i < d->num_golden; i++) {
        /* index -> vector string, first input = MSB (same order as the twin) */
        for (uint8_t b = 0; b < d->num_inputs; b++)
            vector[b] = ((i >> (d->num_inputs - 1 - b)) & 1) ? '1' : '0';
        vector[d->num_inputs] = '\0';

        ApplyVectorAndRead(vector, response);

        uint8_t got = 0;
        for (uint8_t k = 0; k < d->num_outputs; k++)
            if (response[k] == '1') got |= (uint8_t)(1u << k);

        if (got != d->golden[i]) {
            if (fc->mismatches == 0) {
                fc->first_bad_index = i;
                strcpy(fc->first_bad_vector, vector);
                strcpy(fc->first_bad_got, response);
                for (uint8_t k = 0; k < d->num_outputs; k++)
                    fc->first_bad_expected[k] = (d->golden[i] >> k) & 1 ? '1' : '0';
                fc->first_bad_expected[d->num_outputs] = '\0';
            }
            fc->mismatches++;
        }
        if ((i & 0x0F) == 0) HAL_GPIO_TogglePin(LD2_GPIO_Port, LD2_Pin); /* busy blink */
    }
}

static void RunStandaloneTest(uint8_t ic_index)
{
    const IcDescriptor *d = &IC_REGISTRY[ic_index];
    char ic_line[24];
    char detail[24];
    char uart_line[256]; /* sized for worst case: 64-char signature + vectors */
    snprintf(ic_line, sizeof(ic_line), "IC:%s", d->name);

    OledShow(ic_line, "TESTING", "");
    SetResultLeds(0, 0);
    ConfigurePins(d->pin_config);

    /* Stage 1 -- compacted vectors -> fault signature (names the faulty pin). */
    char signature[SIGNATURE_BUFFER_SIZE];
    signature[0] = '\0';
    for (uint8_t i = 0; i < d->num_vectors; i++) {
        char response[NUM_DUT_PINS + 1];
        ApplyVectorAndRead(d->vectors[i], response);
        strncat(signature, response, sizeof(signature) - strlen(signature) - 1);
    }

    /* Stage 2 -- full exhaustive golden check (all 2^n vectors, ~0.3 s). */
    FullCheckResult fc;
    RunFullCheck(d, &fc);

    HAL_GPIO_WritePin(LD2_GPIO_Port, LD2_Pin, GPIO_PIN_RESET);
    TeardownAllPins(); /* leave nothing driven after the test */

    DiagnosisResult res;
    LookupSignature(d, signature, &res);
    last_result = res;

    if (fc.mismatches == 0) {
        /* Every one of the 2^n input combinations matched the digital twin. */
        snprintf(detail, sizeof(detail), "%u/%u OK", fc.total, fc.total);
        OledShow(ic_line, "PASS", detail);
        SetResultLeds(1, 0);
        display_state = DISPLAY_PASS;
        snprintf(uart_line, sizeof(uart_line), "IC:%s RESULT:PASS FULL:%u/%u SIG:%s\n",
                 d->name, fc.total, fc.total, signature);
    }
    else if (res.match_found && res.is_fault) {
        /* Behaviour matches a known single stuck-at fault -> name it. */
        OledShow(ic_line, "FAULT", res.displays[0]);
        SetResultLeds(0, 1);
        cycle_index = 0;
        last_cycle_tick = HAL_GetTick();
        display_state = (res.num_candidates > 1) ? DISPLAY_FAULT_CYCLING : DISPLAY_FAULT_SINGLE;
        snprintf(uart_line, sizeof(uart_line),
                 "IC:%s RESULT:FAULT %s (%s) FULL_BAD:%u/%u SIG:%s\n",
                 d->name, res.fault_names[0], res.displays[0], fc.mismatches, fc.total, signature);
    }
    else {
        /* Full check failed but it is NOT a single stuck pin: power/ground
         * missing, bridged pins, wrong chip in socket, or multiple faults.
         * (This is exactly the case the 3-vector test alone would miss.) */
        snprintf(detail, sizeof(detail), "Bad %u/%u", fc.mismatches, fc.total);
        OledShow(ic_line, "FAULT", detail);
        SetResultLeds(0, 1);
        display_state = DISPLAY_UNKNOWN;
        snprintf(uart_line, sizeof(uart_line),
                 "IC:%s RESULT:FAULT MULTI/UNKNOWN FULL_BAD:%u/%u FIRST V:%s exp R:%s got R:%s SIG:%s\n",
                 d->name, fc.mismatches, fc.total,
                 fc.first_bad_vector, fc.first_bad_expected, fc.first_bad_got, signature);
    }
    UartSend(uart_line); /* free log for the paper if a PC is attached */
}

/* B1 (PC13) or external button on D2 (PA10), both active LOW. Tap = next IC, hold > 1 s = run test. */
static void PollButton(void)
{
    static uint8_t was_pressed = 0, long_handled = 0;
    static uint32_t press_start = 0;

    uint8_t pressed = (HAL_GPIO_ReadPin(B1_GPIO_Port, B1_Pin) == GPIO_PIN_RESET) ||
                      (HAL_GPIO_ReadPin(EXT_BTN_GPIO_Port, EXT_BTN_Pin) == GPIO_PIN_RESET);

    if (pressed) {
        if (!was_pressed) {
            was_pressed = 1;
            long_handled = 0;
            press_start = HAL_GetTick();
        } else if (!long_handled && (HAL_GetTick() - press_start) > LONG_PRESS_MS) {
            long_handled = 1;
            RunStandaloneTest(selected_ic_index);
        }
    } else {
        if (was_pressed && !long_handled && (HAL_GetTick() - press_start) > 30) { /* 30 ms debounce */
            selected_ic_index = (uint8_t)((selected_ic_index + 1) % NUM_SUPPORTED_ICS);
            SetResultLeds(0, 0);
            ShowSelectedIcPrompt();
        }
        was_pressed = 0;
    }
}

/* ---------------------------------------------------------------- Public */
static void ExtButtonInit(void)
{
    GPIO_InitTypeDef g = {0};
    __HAL_RCC_GPIOA_CLK_ENABLE();
    g.Pin = EXT_BTN_Pin;
    g.Mode = GPIO_MODE_INPUT;
    g.Pull = GPIO_PULLUP;          /* idle HIGH, pressed = LOW */
    HAL_GPIO_Init(EXT_BTN_GPIO_Port, &g);
}

void App_Init(void)
{
    ExtButtonInit();
    memset(pin_config, 'X', NUM_DUT_PINS);
    TeardownAllPins();
    SetResultLeds(0, 0);

    I2C_BusRecover();         /* clear a stuck bus left over from a mid-transfer reset */
    ShowSelectedIcPrompt();   /* also initialises the OLED */

    UartSend("ATLAS-DIAG ready\n");
    UartSend(OledPresent() ? "OLED:OK\n" : "OLED:NOT FOUND (check SDA/SCL/VCC)\n");
}

void App_Loop(void)
{
    PollUart();
    PollButton();

    /* Aliased fault: several faults share one signature -- cycle through
     * every candidate instead of silently picking one. */
    if (display_state == DISPLAY_FAULT_CYCLING &&
        (HAL_GetTick() - last_cycle_tick) >= STANDALONE_CYCLE_MS) {
        cycle_index = (uint8_t)((cycle_index + 1) % last_result.num_candidates);
        OledDetail(last_result.displays[cycle_index]);
        last_cycle_tick = HAL_GetTick();
    }
}
