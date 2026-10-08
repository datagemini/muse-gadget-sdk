# Copyright (c) Meta Platforms, Inc. and affiliates.
# SPDX-License-Identifier: Apache-2.0
"""Exercise production PSRAM reply handlers with host-side event sinks."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
JSON = Path(os.environ.get(
    'CJSON_SOURCE_DIR', ROOT / 'managed_components/espressif__cjson/cJSON'
))


class ChatSession(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        out = Path(cls.tmp.name)
        source = (ROOT / 'components/muse/muse_chat_session.cpp').read_text()
        constants = source[source.index('#define MIC_RATE'):source.index('/* ---- Voice task')]
        types = source[source.index('enum phase_t'):source.index('/* 10 KB')]
        handlers = source[source.index('static int find_msg('):source.index('static void on_chat_ack(')]
        recovery = source[source.index('static bool retry_empty_voice_reply('):source.index('/* ---- Inbound dispatch ---- */')]
        reset = source[source.index('static bool turn_start('):source.index('static void turn_begin(')]
        code = r'''
#include <cassert>
#include <cctype>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <initializer_list>
#include "host_compat.h"
#include "cJSON.h"
#include "minimp3.h"
#include "muse_chat_priv.h"
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGW(...) ((void)0)
''' + constants + types + r'''
static turn_t s_turn;
static char s_reply_shown[EV_TEXT];
static int64_t s_last_seq, s_marks[4];
static int captions, console_events, retries;
static int64_t clock_us = 12345;
static char submitted[TEXT_MAX];
static bool fail_submit;
enum mark_t { M_TEXT, M_DONE };
static void mark(mark_t) {}
static int64_t now_us() { return clock_us; }
static void emit(muse_hatch_ev_t type, const char *) {
    if (type == MUSE_HATCH_EV_REPLY) captions++;
}
void muse_hatch_console(const char *, const char *, const char *, ...) { console_events++; }
void muse_hatch_tail_words(const char *text, char *out, size_t cap) { strlcpy(out, text, cap); }
bool muse_hatch_caption_at(const char *text, size_t, char *out, size_t cap) {
    strlcpy(out, text, cap); return text[0];
}
static void turn_finish() { s_turn.phase = P_IDLE; }
static void turn_fail(const char *) { turn_finish(); }
static bool ensure_connected() { return true; }
bool muse_hatch_configured() { return true; }
static void resampler_init(resampler_t *, int, int) {}
''' + reset + handlers + r'''
static void send_reset(int64_t) {}
static void on_dictation_end(bool) {}
static void log_marks() {}
static void turn_done(bool) { turn_finish(); }
static void send_chat(const char *text, const char *modality) {
    assert(!strcmp(modality, "text"));
    strlcpy(submitted, text, sizeof(submitted)); retries++;
    if (fail_submit) { turn_fail("CAN'T REACH MUSE"); return; }
    s_turn.chat_us = s_turn.last_event_us = now_us();
    s_turn.phase = P_WAIT_REPLY;
}
''' + recovery + r'''
static void begin(bool typed = false) {
    assert(turn_start(s_turn.gen + 1, typed));
    s_turn.phase = P_WAIT_REPLY;
    s_turn.acked = true;
    strlcpy(s_turn.user_ids[0], "note", sizeof(s_turn.user_ids[0]));
    strlcpy(s_turn.user_ids[1], "parent", sizeof(s_turn.user_ids[1]));
    captions = console_events = 0;
}
static void event(const char *kind, const char *id, const char *parent = "", const char *text = "") {
    cJSON *root = cJSON_CreateObject(), *payload = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "event");
    cJSON_AddStringToObject(root, "event", kind);
    cJSON_AddItemToObject(root, "payload", payload);
    cJSON_AddStringToObject(payload, "message_id", id);
    if (parent[0]) cJSON_AddStringToObject(payload, "reply_to_message_id", parent);
    cJSON_AddStringToObject(payload, "text", text);
    cJSON_AddStringToObject(payload, "display_text", text);
    on_event(root);
    cJSON_Delete(root);
}
static void rejected_deltas() {
    for (bool typed : {false, true}) {
        begin(typed);
        event("delta.message_start", "other", "elsewhere");
        event("delta.text_append", "other", "", "Wrong reply");
        event("delta.message_done", "other");
        event("message.assistant", "other", "", "Wrong final");
        assert(!s_turn.nmsgs && !captions && !console_events);
        assert(!s_turn.last_content_us && !s_turn.last_event_us);
        event("delta.message_start", "reply", "note");
        event("delta.text_append", "reply", "", "Our reply");
        event("delta.message_done", "reply");
        assert(s_turn.nmsgs == 1 && s_turn.msgs[0].done && s_turn.msgs[0].len == 9);
        assert(typed ? console_events == 2 : captions == 1);
        begin(typed);
        event("message.assistant", "other", "", "New turn");
        assert(s_turn.nmsgs == 1 && s_turn.msgs[0].done);
    }
}
static void valid_parents() {
    begin();
    event("message.assistant", "first", "note", "First");
    event("message.assistant", "second", "parent", "Second");
    event("message.assistant", "third", "first", "Third");
    event("message.assistant", "fourth", "", "Live");
    assert(s_turn.nmsgs == 4);
    for (int i = 0; i < s_turn.nmsgs; i++) assert(s_turn.msgs[i].done);
    cJSON *payload = cJSON_Parse("{\"parent_message_id\":\"elsewhere\"}");
    assert(bind_msg("fallback", payload) == -1);
    cJSON_Delete(payload);
    event("message.assistant", "fallback", "", "Wrong final");
    assert(s_turn.nmsgs == 4);
}
static void bounded_rejections() {
    begin();
    event("delta.message_start", "reply", "note");
    char id[20];
    for (int i = 0; i < 9; i++) { /* exceed the eight rejected IDs retained per turn */
        snprintf(id, sizeof(id), "other%d", i);
        event("delta.message_start", id, "elsewhere");
    }
    event("message.assistant", "other0", "", "Wrong final");
    event("message.assistant", id, "", "Overflow final");
    assert(s_turn.nmsgs == 1 && !captions && !console_events);
    event("delta.text_append", "reply", "", "Our reply");
    event("delta.message_done", "reply");
    event("message.assistant", "second", "note", "Second");
    assert(s_turn.nmsgs == 2 && s_turn.msgs[0].done && s_turn.msgs[1].done);
}

static void voice_transcript(const char *text = "你好，今天怎么样？\n[file:audio/wav voice_note.wav]") {
    event("message.user", "note", "", text);
}
static void settle() { clock_us += SETTLE_US + 1; check_turn(); }
static void recover_voice() {
    begin(); voice_transcript();
    event("delta.message_done", "empty", "note");
    check_turn(); assert(!retries); /* no immediate retry */
    settle(); assert(retries == 1 && s_turn.text_retry && !s_turn.text);
    assert(!strcmp(submitted, "你好，今天怎么样？"));
    assert(!s_turn.nmsgs && !s_turn.acked && s_turn.phase == P_WAIT_REPLY);
    event("message.assistant", "empty", "", "Late original reply");
    event("message.assistant", "late", "note", "Late sibling");
    assert(!s_turn.nmsgs);
    s_turn.acked = true; strlcpy(s_turn.user_ids[0], "retry", sizeof(s_turn.user_ids[0]));
    event("message.assistant", "good", "retry", "Recovered");
    s_turn.msgs[0].tts = TTS_FINISHED;
    settle(); assert(s_turn.phase == P_IDLE && retries == 1);
}
static void bounded_retry() {
    begin(); voice_transcript(); event("delta.message_done", "empty", "note"); settle();
    s_turn.acked = true; strlcpy(s_turn.user_ids[0], "retry", sizeof(s_turn.user_ids[0]));
    event("delta.message_done", "empty-again", "retry"); settle();
    assert(retries == 1 && s_turn.phase == P_IDLE);
    begin(); assert(!s_turn.text_retry && !s_turn.transcript[0]);
}
static void no_unsafe_retry() {
    begin(); voice_transcript();
    event("message.assistant", "normal", "note", "Valid answer");
    s_turn.msgs[0].tts = TTS_FINISHED; settle(); assert(!retries && s_turn.phase == P_IDLE);
    begin(true); event("delta.message_done", "empty", "note"); settle(); assert(!retries);
    begin(); event("message.user", "other", "", "Wrong transcript");
    event("delta.message_done", "empty", "note"); settle(); assert(!retries && s_turn.phase == P_IDLE);
    begin();
    cJSON *pending = cJSON_Parse(R"({"type":"event","event":"message.user","payload":{"message_id":"note","display_text":"Partial transcription","display_text_ready":false}})");
    on_event(pending); cJSON_Delete(pending);
    event("delta.message_done", "empty", "note"); settle(); assert(!retries);
    begin(); voice_transcript("[Voice note]");
    event("delta.message_done", "empty", "note"); settle(); assert(!retries);
    begin(); voice_transcript(); clock_us += REPLY_TIMEOUT_US + 1; check_turn();
    assert(!retries && s_turn.phase == P_IDLE); /* no reply/timeout is not an empty final */
    begin(); voice_transcript(); s_turn.transcript_id[0] = 0;
    event("delta.message_done", "empty", "note"); settle(); assert(!retries);
    begin(); char large[TEXT_MAX+4]; memset(large, 'x', sizeof(large)-1); large[sizeof(large)-1]=0;
    voice_transcript(large); event("delta.message_done", "empty", "note"); settle(); assert(!retries);
}
static void deferred_and_failed_retry() {
    begin(); event("delta.message_done", "empty", "note"); voice_transcript();
    s_turn.agent_busy = true; settle(); assert(!retries);
    s_turn.agent_busy = false; check_turn(); assert(retries == 1);
    begin(); voice_transcript(); event("delta.message_done", "empty", "note");
    fail_submit = true; settle(); assert(retries == 2 && s_turn.phase == P_IDLE);
}
static void early_transcript_and_partial() {
    begin(); s_turn.acked = false; voice_transcript(); s_turn.acked = true;
    event("delta.message_done", "empty", "note"); settle(); assert(retries == 1);
    begin(); s_turn.acked = false; event("message.user", "other", "", "Wrong early transcript");
    s_turn.acked = true; event("delta.message_done", "empty", "note"); settle(); assert(retries == 1);
    begin(); voice_transcript(); event("delta.text_append", "partial", "note", "Some text");
    clock_us += SETTLE_US + 1; check_turn(); assert(retries == 1 && s_turn.phase == P_WAIT_REPLY);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    switch (atoi(argv[1])) {
    case 0: rejected_deltas(); break;
    case 1: valid_parents(); break;
    case 2: bounded_rejections(); break;
    case 3: recover_voice(); break;
    case 4: bounded_retry(); break;
    case 5: no_unsafe_retry(); break;
    case 6: deferred_and_failed_retry(); break;
    case 7: early_transcript_and_partial(); break;
    default: return 2;
    }
}
'''
        (out / 'session.cpp').write_text(code)
        flags = ['-Wall', '-Wextra', '-Werror', '-I', str(JSON),
                 '-I', str(ROOT / 'tests'), '-I', str(ROOT / 'components/muse'),
                 '-I', str(ROOT / 'components/minimp3/include')]
        commands = [
            [*shlex.split(os.environ.get('CC', 'cc')), '-std=c11', *flags,
             '-c', str(JSON / 'cJSON.c'), '-o', str(out / 'cjson.o')],
            [*shlex.split(os.environ.get('CXX', 'c++')), '-std=gnu++17', *flags,
             str(out / 'session.cpp'), str(out / 'cjson.o'), '-o', str(out / 'session')],
        ]
        for command in commands:
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
        cls.binary = out / 'session'

    def run_case(self, case):
        result = subprocess.run([str(self.binary), str(case)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rejected_deltas_do_not_emit_or_complete_voice_or_typed_replies(self):
        self.run_case(0)

    def test_ack_parent_reply_chain_and_parentless_messages_still_work(self):
        self.run_case(1)

    def test_rejection_capacity_preserves_correlation(self):
        self.run_case(2)

    def test_empty_voice_reply_retries_correlated_transcript_and_drops_stale_replies(self):
        self.run_case(3)

    def test_empty_reply_retry_is_limited_to_one_and_resets_next_turn(self):
        self.run_case(4)

    def test_normal_typed_uncorrelated_placeholder_long_and_timeout_turns_do_not_retry(self):
        self.run_case(5)

    def test_busy_agent_defers_retry_and_send_failure_ends_turn(self):
        self.run_case(6)

    def test_transcript_before_ack_and_partial_reply_do_not_break_correlation(self):
        self.run_case(7)
