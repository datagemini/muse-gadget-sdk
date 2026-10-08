"""Test AXP2101 shutdown configuration and Waveshare PWR/BOOT event routing."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WavesharePowerKey(unittest.TestCase):
    def test_hardware_shutdown_registers_and_pmu_button_edges(self):
        pmu = (ROOT / 'components/muse/muse_pmu.c').read_text()
        board = (ROOT / 'components/muse/boards/board_waveshare_s3_175c.c').read_text()
        constants = pmu[pmu.index('#define AXP2101_ADDR'):pmu.index('static i2c_master_dev_handle_t')]
        init = pmu[pmu.index('esp_err_t muse_pmu_init('):pmu.index('esp_err_t muse_pmu_keep_rails(')]
        poll = board[board.index('static unsigned poll_buttons('):board.index('static const muse_board_t')]
        self.assertIn('muse_pmu_init(bsp_i2c_get_handle(), true)', board)
        self.assertNotIn('s_pwr', board)
        self.assertNotIn('.wait_buttons =', board)  # PMU has no GPIO wake interrupt
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#define ESP_OK 0
#define ESP_LOGI(...) ((void)0)
#define ESP_RETURN_ON_ERROR(call, ...) do { int e=(call); if(e) return e; } while(0)
#define MUSE_PMU_KEY_PRESS 1
#define MUSE_PMU_KEY_RELEASE 2
#define MUSE_BTN_AUX_PRESS 4
#define MUSE_BTN_AUX_RELEASE 8
#define PMU_KEY_EVERY 2
#define I2C_ADDR_BIT_LEN_7 7
typedef int esp_err_t;
typedef int i2c_master_bus_handle_t;
typedef int i2c_master_dev_handle_t;
typedef struct { int dev_addr_length, device_address, scl_speed_hz; } i2c_device_config_t;
static i2c_master_dev_handle_t s_dev;
static uint8_t regs[256];
static int operations, fail_at;
static int i2c_master_bus_add_device(int bus,const i2c_device_config_t *cfg,int *dev) {
    assert(bus == 1 && cfg->device_address == 0x34 && cfg->scl_speed_hz == 400000);
    *dev=1; return ESP_OK;
}
static int rd(uint8_t reg,uint8_t *v) {
    if(++operations==fail_at) return -1;
    *v=regs[reg]; return ESP_OK;
}
static int wr(uint8_t reg,uint8_t v) {
    if(++operations==fail_at) return -1;
    regs[reg]=v; return ESP_OK;
}
static int s_boot;
static unsigned boot_ev,key_ev;
static unsigned muse_gpio_button_poll(int *b) { assert(b==&s_boot); return boot_ev; }
static unsigned muse_pmu_poll_key(void) { unsigned ev=key_ev; key_ev=0; return ev; }
''' + constants + init + poll + r'''
int main(void) {
    for (int initial=0;initial<256;initial++) {
        regs[REG_PWROFF_EN]=initial; regs[REG_IRQ_LEVEL]=0xa3;
        operations=0; assert(muse_pmu_init(1,true)==ESP_OK);
        assert(regs[REG_PWROFF_EN]==((initial|2)&~1));
        assert(regs[REG_IRQ_LEVEL]==0xaf); /* 10 s, preserve unrelated bits */
        assert((regs[REG_INTEN2]&PKEY_ALL)==PKEY_ALL);
    }
    for (int fail=1;fail<=5;fail++) {
        operations=0; fail_at=fail; assert(muse_pmu_init(1,true)==-1);
    }
    fail_at=0;
    for(unsigned b=0;b<4;b++) for(unsigned key=0;key<4;key++) {
        boot_ev=b; key_ev=key;
        unsigned events=poll_buttons()|poll_buttons();
        assert((events&3)==b); assert(((events>>2)&3)==key);
        assert(!key_ev); /* polling drains PMU latch, including while asleep */
    }
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as t:
            source = Path(t) / 'power.c'; source.write_text(code)
            binary = source.with_suffix('')
            subprocess.run(['cc', '-std=c11', str(source), '-o', str(binary)],
                           check=True, capture_output=True)
            run = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
