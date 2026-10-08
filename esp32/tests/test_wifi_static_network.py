"""Exercise the fixed-IP initializer against a DHCP/DNS netif fake."""
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StaticNetwork(unittest.TestCase):
    def test_static_address_dns_order_errors_and_other_boards(self):
        source = (ROOT / 'main/wifi_mgr.c').read_text()
        helper = source[source.index('static esp_err_t configure_static_network('):source.index('void wifi_mgr_init(')]
        init = source[source.index('void wifi_mgr_init('):source.index('bool wifi_mgr_connect(')]
        self.assertLess(init.index('configure_static_network()'), init.index('esp_wifi_start()'))
        code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <arpa/inet.h>
#define ESP_OK 0
#define ESP_ERR_ESP_NETIF_DHCP_ALREADY_STOPPED 1
#define ESP_IPADDR_TYPE_V4 0
#define ESP_LOGI(...) ((void)0)
typedef int esp_err_t;
typedef struct { uint32_t addr; } esp_ip4_addr_t;
typedef struct { esp_ip4_addr_t ip, netmask, gw; } esp_netif_ip_info_t;
typedef struct { struct { int type; union { esp_ip4_addr_t ip4; } u_addr; } ip; } esp_netif_dns_info_t;
typedef enum { ESP_NETIF_DNS_MAIN, ESP_NETIF_DNS_BACKUP, ESP_NETIF_DNS_FALLBACK } esp_netif_dns_type_t;
static int netif, *s_sta_netif = &netif, stopped, calls, fail_step;
static esp_netif_ip_info_t ip;
static uint32_t dns[3];
static esp_err_t step(void) { return ++calls == fail_step ? -99 : ESP_OK; }
static esp_err_t esp_netif_dhcpc_stop(int *n) {
    assert(n == s_sta_netif);
    if (step()) return -99;
    if (stopped) return ESP_ERR_ESP_NETIF_DHCP_ALREADY_STOPPED;
    stopped = 1; return ESP_OK;
}
static esp_err_t esp_netif_str_to_ip4(const char *s, esp_ip4_addr_t *dst) {
    if (step()) return -99;
    return inet_pton(AF_INET, s, &dst->addr) == 1 ? ESP_OK : -99;
}
static esp_err_t esp_netif_set_ip_info(int *n, const esp_netif_ip_info_t *info) {
    assert(n == s_sta_netif && stopped);
    if (step()) return -99;
    ip = *info; memset(dns, 0, sizeof(dns)); return ESP_OK;
}
static esp_err_t esp_netif_set_dns_info(int *n, esp_netif_dns_type_t type, esp_netif_dns_info_t *info) {
    assert(n == s_sta_netif && stopped && ip.ip.addr && info->ip.type == ESP_IPADDR_TYPE_V4);
    if (step()) return -99;
    dns[type] = info->ip.u_addr.ip4.addr; return ESP_OK;
}
''' + helper + r'''
int main(void) {
    assert(configure_static_network() == ESP_OK);
#if CONFIG_MUSE_BOARD_WAVESHARE_S3_175C
    assert(stopped && ip.ip.addr == htonl(0x0a0000e4));
    assert(ip.netmask.addr == htonl(0xffffff00));
    assert(ip.gw.addr == htonl(0x0a00000a));
    for (int i=0; i<3; i++) assert(dns[i] == htonl(0x0a00000a));
    calls=0; assert(configure_static_network() == ESP_OK); /* already stopped is valid */
    for (int i=0; i<3; i++) assert(dns[i] == htonl(0x0a00000a));
    for (int i=1; i<=9; i++) {
        stopped=0; calls=0; fail_step=i; memset(&ip, 0, sizeof(ip));
        assert(configure_static_network() == -99 && calls == i);
    }
#else
    assert(!calls && !stopped && !ip.ip.addr);
#endif
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / 'network.c'
            path.write_text(code)
            for enabled in (0, 1):
                binary = Path(t) / f'network-{enabled}'
                subprocess.run(['cc', '-std=c11', f'-DCONFIG_MUSE_BOARD_WAVESHARE_S3_175C={enabled}',
                                str(path), '-o', str(binary)], check=True, capture_output=True)
                result = subprocess.run([str(binary)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
