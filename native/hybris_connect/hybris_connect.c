#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/random.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>
#include <pthread.h>

#include <hybris/common/binding.h>
#include <hybris/common/hooks.h>

#define MAX_FIELD_BYTES 4096U
#define MAX_COMMAND_BYTES 32768U
#define CONNECT_TYPE_NORMAL 126
#define CONNECT_STATE_ONLINE 3
#define COMMAND_CHANNEL 0
#define VIDEO_CHANNEL 1
#define TALKBACK_CHANNEL 3
#define TALKBACK_CHUNK_BYTES 640U
#define TALKBACK_QUEUE_CAPACITY 16U
#define TALKBACK_PACING_INTERVAL_MS 40LL
#define LOGIN_RESPONSE 24577U
#define READ_TIMEOUT_MS 500
#define LOGIN_TIMEOUT_SECONDS 35
#define STREAM_TIMEOUT_SECONDS 45
#define VIDEO_HEADER_BYTES 32U
#define TALKBACK_NATIVE_HEADER_BYTES 32U
#define TALKBACK_NATIVE_FRAME_BYTES (TALKBACK_NATIVE_HEADER_BYTES + TALKBACK_CHUNK_BYTES)
#define MAX_VIDEO_FRAME_BYTES (8U * 1024U * 1024U)
#define MIN_H264_FRAMES 3U
#define MIN_H264_BYTES 1024U

typedef void *(*client_create_fn)(const char *, const char *);
typedef int (*client_connect_fn)(void *, int, const char *, int);
typedef bool (*client_login_fn)(void *, const char *, const char *);
typedef bool (*client_write_cgi_fn)(void *, const char *, int);
typedef bool (*client_write_fn)(void *, int, const void *, int, int);
typedef int (*client_read_fn)(void *, int, void *, int, int, int *);
typedef bool (*client_disconnect_fn)(void *);
typedef void (*client_destroy_fn)(void *);

static uintptr_t stack_guard;
static volatile sig_atomic_t stream_running = 1;
static int talkback_fd = -1;
static void *talkback_client = NULL;
static client_write_fn talkback_write = NULL;
static pthread_t talkback_thread;
static bool talkback_started = false;
static pthread_t talkback_pacer_thread;
static bool talkback_pacer_started = false;
static const char *talkback_uid = NULL;
static int talkback_connect_state = -1;
static bool talkback_authenticated = false;

typedef struct {
    unsigned long long input_message_count;
    unsigned long long input_message_bytes;
    unsigned long long native_chunk_count;
    unsigned long long native_chunk_bytes;
    unsigned long long full_chunk_count;
    unsigned long long discarded_residual_bytes_total;
    unsigned long long write_attempt_count;
    unsigned long long write_success_count;
    unsigned long long write_failure_count;
    unsigned long long short_header_read_count;
    unsigned long long malformed_header_count;
    unsigned long long incomplete_payload_count;
    unsigned long long invalid_length_count;
    unsigned long long write_elapsed_ms_total;
    unsigned long long write_since_previous_ms_total;
    long long previous_write_ms;
    unsigned int first_write_timing_count;
    bool first_success_reported;
    bool first_failure_reported;
    unsigned long long pacing_overflow_count;
    unsigned int pacing_queue_depth_max;
} talkback_diagnostics_t;

static talkback_diagnostics_t talkback_diagnostics;
static const unsigned char talkback_native_header[TALKBACK_NATIVE_HEADER_BYTES] = {
    0x55, 0xaa, 0x15, 0xa8,
    0x08, 0x01, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x80, 0x02, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x07, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00,
};
typedef struct {
    unsigned char payload[TALKBACK_CHUNK_BYTES];
} talkback_chunk_t;

static struct {
    talkback_chunk_t chunks[TALKBACK_QUEUE_CAPACITY];
    size_t head;
    size_t tail;
    size_t count;
    bool stop;
    pthread_mutex_t mutex;
    pthread_cond_t condition;
} talkback_queue = {
    .mutex = PTHREAD_MUTEX_INITIALIZER,
    .condition = PTHREAD_COND_INITIALIZER,
};
extern void __stack_chk_fail(void);

static long long diagnostic_started_ms(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) return 0;
    return (long long)value.tv_sec * 1000LL + value.tv_nsec / 1000000LL;
}

static void diagnostic_prefix(void) {
    struct timespec now;
    struct tm local;
    char date[32] = "";
    char offset[8] = "";
    if (clock_gettime(CLOCK_REALTIME, &now) == 0 &&
        localtime_r(&now.tv_sec, &local) != NULL) {
        strftime(date, sizeof(date), "%Y-%m-%d %H:%M:%S", &local);
        strftime(offset, sizeof(offset), "%z", &local);
        if (strlen(offset) == 5) {
            char colonized[8];
            snprintf(colonized, sizeof(colonized), "%.3s:%.2s", offset, offset + 3);
            fprintf(stderr, "%s.%03ld %s process_id=%s ", date,
                    now.tv_nsec / 1000000L, colonized,
                    getenv("OKAM_PROCESS_ID") != NULL ? getenv("OKAM_PROCESS_ID") : "-");
            return;
        }
    }
    fprintf(stderr, "1970-01-01 00:00:00.000 +00:00 process_id=%s ",
            getenv("OKAM_PROCESS_ID") != NULL ? getenv("OKAM_PROCESS_ID") : "-");
}

static void diagnostic_event(const char *event, const char *uid, const char *extra) {
    static long long started = 0;
    if (started == 0) started = diagnostic_started_ms();
    diagnostic_prefix();
    fprintf(stderr, "native_diag event=%s camera_uid=%s session_id=%s "
                    "session_generation=%s elapsed_ms=%lld%s%s\n",
            event, uid != NULL ? uid : "-",
            getenv("OKAM_DIAG_SESSION_ID") != NULL ? getenv("OKAM_DIAG_SESSION_ID") : "-",
            getenv("OKAM_DIAG_SESSION_GENERATION") != NULL ? getenv("OKAM_DIAG_SESSION_GENERATION") : "-",
            diagnostic_started_ms() - started,
            extra != NULL && extra[0] != '\0' ? " " : "",
            extra != NULL ? extra : "");
    fflush(stderr);
}

static bool debug_credentials_enabled(void) {
    const char *value = getenv("OKAM_DEBUG_CREDENTIALS");
    return value != NULL && strcmp(value, "1") == 0;
}

static void print_debug_credential(const char *stage, const char *uid, const char *password) {
    static const char hex[] = "0123456789abcdef";
    size_t length = strlen(password);
    diagnostic_prefix();
    fprintf(stderr, "%s uid=%s username_present=true password_present=true "
                    "username='admin' password='", stage, uid);
    for (size_t i = 0; i < length; ++i) {
        unsigned char byte = (unsigned char)password[i];
        if (byte == '\\' || byte == '\'') {
            fprintf(stderr, "\\\\%c", byte);
        } else if (byte >= 0x20 && byte < 0x7f) {
            fputc(byte, stderr);
        } else {
            fprintf(stderr, "\\x%c%c", hex[byte >> 4], hex[byte & 0x0f]);
        }
    }
    fputs("' password_length=", stderr);
    fprintf(stderr, "%zu password_hex=", length);
    for (size_t i = 0; i < length; ++i) {
        unsigned char byte = (unsigned char)password[i];
        fprintf(stderr, "%c%c", hex[byte >> 4], hex[byte & 0x0f]);
    }
    fputc('\n', stderr);
    fflush(stderr);
}

static void stop_streaming(int signal_number) {
    (void)signal_number;
    stream_running = 0;
}

static void *okam_hook(const char *symbol_name, const char *requester) {
    (void)requester;
    if (strcmp(symbol_name, "__stack_chk_guard") == 0) return &stack_guard;
    if (strcmp(symbol_name, "__stack_chk_fail") == 0) return (void *)&__stack_chk_fail;
    if (strcmp(symbol_name, "usleep") == 0) return (void *)&usleep;
    if (strcmp(symbol_name, "gettimeofday") == 0) return (void *)&gettimeofday;
    if (strcmp(symbol_name, "time") == 0) return (void *)&time;
    if (strcmp(symbol_name, "difftime") == 0) return (void *)&difftime;
    if (strcmp(symbol_name, "sleep") == 0) return (void *)&sleep;
    if (strcmp(symbol_name, "nanosleep") == 0) return (void *)&nanosleep;
    if (strcmp(symbol_name, "__vsprintf_chk") == 0 ||
        strcmp(symbol_name, "__vsnprintf_chk") == 0) {
        return dlsym(RTLD_DEFAULT, symbol_name);
    }
    return NULL;
}

static bool initialize_stack_guard(void) {
    ssize_t received;
    do {
        received = getrandom(&stack_guard, sizeof(stack_guard), 0);
    } while (received < 0 && errno == EINTR);
    return received == (ssize_t)sizeof(stack_guard) && stack_guard != 0;
}

static bool read_exact(void *buffer, size_t size) {
    unsigned char *cursor = buffer;
    while (size > 0) {
        size_t received = fread(cursor, 1, size, stdin);
        if (received == 0) return false;
        cursor += received;
        size -= received;
    }
    return true;
}

static char *read_field(bool allow_empty) {
    uint32_t network_size;
    if (!read_exact(&network_size, sizeof(network_size))) return NULL;
    uint32_t size = ntohl(network_size);
    if ((!allow_empty && size == 0) || size > MAX_FIELD_BYTES) return NULL;
    char *value = calloc((size_t)size + 1, 1);
    if (value == NULL || !read_exact(value, size)) {
        free(value);
        return NULL;
    }
    for (uint32_t i = 0; i < size; ++i) {
        if ((unsigned char)value[i] < 0x20) {
            free(value);
            return NULL;
        }
    }
    return value;
}

static uint16_t read_le16(const unsigned char *buffer) {
    return (uint16_t)buffer[0] | ((uint16_t)buffer[1] << 8);
}

static bool read_client_exact(client_read_fn client_read, void *client, int channel,
                              unsigned char *buffer, size_t size,
                              time_t deadline) {
    size_t offset = 0;
    while (offset < size && time(NULL) <= deadline) {
        int received = 0;
        int result = client_read(client, channel, buffer + offset,
                                 (int)(size - offset), READ_TIMEOUT_MS, &received);
        if (received > 0 && (size_t)received <= size - offset) offset += (size_t)received;
        if (offset == size) return true;
        if (result != -3 && result < 0) return false;
    }
    return false;
}

static uint32_t read_le32(const unsigned char *buffer) {
    return (uint32_t)buffer[0] | ((uint32_t)buffer[1] << 8) |
           ((uint32_t)buffer[2] << 16) | ((uint32_t)buffer[3] << 24);
}

static bool is_login_response(uint16_t command) {
    return command == LOGIN_RESPONSE;
}

static bool parse_result_code(const unsigned char *payload, size_t size, int *result) {
    static const char key[] = "result";
    for (size_t i = 0; i + sizeof(key) - 1 < size; ++i) {
        if (memcmp(payload + i, key, sizeof(key) - 1) != 0) continue;
        size_t cursor = i + sizeof(key) - 1;
        while (cursor < size && (payload[cursor] == ' ' || payload[cursor] == '\t')) cursor++;
        if (cursor >= size || payload[cursor++] != '=') continue;
        while (cursor < size && (payload[cursor] == ' ' || payload[cursor] == '\t' ||
                                 payload[cursor] == '\"' || payload[cursor] == '\'')) cursor++;
        int sign = 1;
        if (cursor < size && payload[cursor] == '-') {
            sign = -1;
            cursor++;
        }
        if (cursor >= size || payload[cursor] < '0' || payload[cursor] > '9') continue;
        int value = 0;
        while (cursor < size && payload[cursor] >= '0' && payload[cursor] <= '9') {
            if (value > 100000) return false;
            value = value * 10 + payload[cursor++] - '0';
        }
        *result = sign * value;
        return true;
    }
    return false;
}

static bool await_login_response(client_read_fn client_read, void *client,
                                 uint16_t *response_command, int *result_code) {
    time_t deadline = time(NULL) + LOGIN_TIMEOUT_SECONDS;
    while (time(NULL) <= deadline) {
        unsigned char header[8];
        if (!read_client_exact(client_read, client, COMMAND_CHANNEL,
                               header, sizeof(header), deadline)) return false;
        uint16_t magic = read_le16(header);
        uint16_t command = read_le16(header + 2);
        uint16_t length = read_le16(header + 4);
        if (magic != 0x0a01U || length > MAX_COMMAND_BYTES) return false;
        unsigned char *payload = calloc((size_t)length + 1, 1);
        if (payload == NULL) return false;
        bool read_ok = length == 0 ||
            read_client_exact(client_read, client, COMMAND_CHANNEL,
                              payload, length, deadline);
        bool parsed = false;
        int code = 0;
        if (read_ok && is_login_response(command)) parsed = parse_result_code(payload, length, &code);
        memset(payload, 0, (size_t)length + 1);
        free(payload);
        if (!read_ok) return false;
        if (parsed) {
            *response_command = command;
            *result_code = code;
            return true;
        }
    }
    return false;
}

static bool inspect_h264_payload(const unsigned char *payload, size_t size,
                                 bool *keyframe_seen) {
    bool valid = false;
    for (size_t i = 0; i + 4 < size; ++i) {
        size_t nal = 0;
        if (payload[i] == 0 && payload[i + 1] == 0 && payload[i + 2] == 1) {
            nal = i + 3;
        } else if (i + 4 < size && payload[i] == 0 && payload[i + 1] == 0 &&
                   payload[i + 2] == 0 && payload[i + 3] == 1) {
            nal = i + 4;
        }
        if (nal == 0 || nal >= size) continue;
        uint8_t type = payload[nal] & 0x1fU;
        if (type >= 1U && type <= 12U) {
            valid = true;
            if (type == 5U || type == 7U) *keyframe_seen = true;
        }
    }
    return valid;
}

static void diagnostic_h264_units(const unsigned char *payload, size_t size, const char *uid) {
    static bool sps_seen = false;
    static bool pps_seen = false;
    static bool idr_seen = false;
    for (size_t i = 0; i + 4 < size; ++i) {
        size_t nal = 0;
        if (payload[i] == 0 && payload[i + 1] == 0 && payload[i + 2] == 1) nal = i + 3;
        else if (payload[i] == 0 && payload[i + 1] == 0 && payload[i + 2] == 0 && payload[i + 3] == 1) nal = i + 4;
        if (nal == 0 || nal >= size) continue;
        uint8_t type = payload[nal] & 0x1fU;
        if (type == 7U && !sps_seen) { sps_seen = true; diagnostic_event("first_h264_sps", uid, ""); }
        if (type == 8U && !pps_seen) { pps_seen = true; diagnostic_event("first_h264_pps", uid, ""); }
        if (type == 5U && !idr_seen) { idr_seen = true; diagnostic_event("first_h264_idr", uid, ""); }
    }
}

static bool await_h264_frames(client_read_fn client_read, void *client,
                              unsigned int *frames, unsigned long long *bytes,
                              bool *keyframe_seen, unsigned int *h265_frames) {
    time_t deadline = time(NULL) + STREAM_TIMEOUT_SECONDS;
    while (time(NULL) <= deadline &&
           (*frames < MIN_H264_FRAMES || *bytes < MIN_H264_BYTES || !*keyframe_seen)) {
        unsigned char header[VIDEO_HEADER_BYTES];
        if (!read_client_exact(client_read, client, VIDEO_CHANNEL,
                               header, sizeof(header), deadline)) return false;
        if (read_le32(header) != 0xa815aa55U) return false;
        uint32_t length = read_le32(header + 16);
        if (length == 0 || length > MAX_VIDEO_FRAME_BYTES) return false;
        unsigned char *payload = malloc(length);
        if (payload == NULL) return false;
        bool read_ok = read_client_exact(client_read, client, VIDEO_CHANNEL,
                                         payload, length, deadline);
        if (!read_ok) {
            memset(payload, 0, length);
            free(payload);
            return false;
        }
        if (header[4] == 0x10U || header[4] == 0x11U) {
            (*h265_frames)++;
        } else if (inspect_h264_payload(payload, length, keyframe_seen)) {
            (*frames)++;
            *bytes += length;
        }
        memset(payload, 0, length);
        free(payload);
    }
    return *frames >= MIN_H264_FRAMES && *bytes >= MIN_H264_BYTES && *keyframe_seen;
}

static bool write_stdout(const unsigned char *payload, size_t size) {
    size_t offset = 0;
    while (offset < size) {
        ssize_t written = write(STDOUT_FILENO, payload + offset, size - offset);
        if (written > 0) {
            offset += (size_t)written;
            continue;
        }
        if (written < 0 && errno == EINTR) continue;
        return false;
    }
    return true;
}

static void talkback_ipc_error(const char *kind, unsigned long long count, ssize_t bytes) {
    if (count != 1 && count % 100 != 0) return;
    char extra[160];
    snprintf(extra, sizeof(extra), "kind=%s count=%llu bytes=%zd",
             kind, count, bytes);
    diagnostic_event("native_talkback_ipc_error", talkback_uid, extra);
}

static void talkback_write_progress(void) {
    if (talkback_diagnostics.write_attempt_count == 0 ||
        talkback_diagnostics.write_attempt_count % 250 != 0) return;
    char extra[512];
    long long now = diagnostic_started_ms();
    long long elapsed = now > 0 ? now : 0;
    double seconds = elapsed > 0 ? (double)elapsed / 1000.0 : 0.001;
    snprintf(extra, sizeof(extra),
             "channel=%d payload_size=640 timeout_ms=2000 write_result=aggregate "
             "write_attempt_count=%llu write_success_count=%llu write_failure_count=%llu "
             "input_message_count=%llu input_message_bytes=%llu native_chunk_count=%llu "
             "native_chunk_bytes=%llu full_chunk_count=%llu discarded_residual_bytes_total=%llu "
             "write_call_elapsed_ms_total=%llu write_since_previous_ms_total=%llu "
             "write_call_elapsed_ms_average=%.3f writes_per_second=%.3f payload_bytes_per_second=%.3f "
             "connected=%s authenticated=%s client_valid=%s connect_state=%d",
             TALKBACK_CHANNEL,
             talkback_diagnostics.write_attempt_count,
             talkback_diagnostics.write_success_count,
             talkback_diagnostics.write_failure_count,
             talkback_diagnostics.input_message_count,
             talkback_diagnostics.input_message_bytes,
             talkback_diagnostics.native_chunk_count,
             talkback_diagnostics.native_chunk_bytes,
             talkback_diagnostics.full_chunk_count,
             talkback_diagnostics.discarded_residual_bytes_total,
             talkback_diagnostics.write_elapsed_ms_total,
             talkback_diagnostics.write_since_previous_ms_total,
             talkback_diagnostics.write_attempt_count > 0
                 ? (double)talkback_diagnostics.write_elapsed_ms_total /
                   (double)talkback_diagnostics.write_attempt_count : 0.0,
             (double)talkback_diagnostics.write_attempt_count / seconds,
             (double)talkback_diagnostics.native_chunk_bytes / seconds,
             talkback_connect_state == CONNECT_STATE_ONLINE ? "true" : "false",
             talkback_authenticated ? "true" : "false",
             talkback_client != NULL ? "true" : "false",
             talkback_connect_state);
    diagnostic_event("native_talkback_write_progress", talkback_uid, extra);
}

static void talkback_write_one(const unsigned char *payload) {
    unsigned char native_frame[TALKBACK_NATIVE_FRAME_BYTES];
    memcpy(native_frame, talkback_native_header, TALKBACK_NATIVE_HEADER_BYTES);
    memcpy(native_frame + TALKBACK_NATIVE_HEADER_BYTES, payload, TALKBACK_CHUNK_BYTES);
    long long started = diagnostic_started_ms();
    long long since_previous = talkback_diagnostics.previous_write_ms > 0 && started > 0
        ? started - talkback_diagnostics.previous_write_ms : -1;
    if (talkback_diagnostics.native_chunk_count == 0) {
        diagnostic_event("native_talkback_native_frame_first", talkback_uid,
                         "channel=3 native_header_bytes=32 audio_payload_bytes=640 "
                         "client_write_bytes=672 native_type=8");
    }
    bool write_result = talkback_write(
        talkback_client, TALKBACK_CHANNEL, native_frame, TALKBACK_NATIVE_FRAME_BYTES, 2000);
    long long finished = diagnostic_started_ms();
    long long call_elapsed = started > 0 && finished >= started ? finished - started : -1;
    talkback_diagnostics.previous_write_ms = finished;
    talkback_diagnostics.write_attempt_count++;
    talkback_diagnostics.native_chunk_count++;
    talkback_diagnostics.native_chunk_bytes += 640;
    if (call_elapsed >= 0) talkback_diagnostics.write_elapsed_ms_total += (unsigned long long)call_elapsed;
    if (since_previous >= 0) talkback_diagnostics.write_since_previous_ms_total += (unsigned long long)since_previous;
    if (write_result) talkback_diagnostics.write_success_count++;
    else talkback_diagnostics.write_failure_count++;
    unsigned long long attempt = talkback_diagnostics.write_attempt_count;
    if (attempt == 1 || (write_result && !talkback_diagnostics.first_success_reported) ||
        (!write_result && !talkback_diagnostics.first_failure_reported)) {
        char extra[512];
        snprintf(extra, sizeof(extra),
                 "channel=%d payload_size=640 timeout_ms=2000 write_result=%s "
                 "write_attempt_count=%llu write_success_count=%llu write_failure_count=%llu "
                 "input_message_count=%llu input_message_bytes=%llu native_chunk_count=%llu "
                 "native_chunk_bytes=%llu full_chunk_count=%llu discarded_residual_bytes_total=%llu "
                 "connected=%s authenticated=%s client_valid=%s connect_state=%d",
                 TALKBACK_CHANNEL, write_result ? "true" : "false", attempt,
                 talkback_diagnostics.write_success_count,
                 talkback_diagnostics.write_failure_count,
                 talkback_diagnostics.input_message_count,
                 talkback_diagnostics.input_message_bytes,
                 talkback_diagnostics.native_chunk_count,
                 talkback_diagnostics.native_chunk_bytes,
                 talkback_diagnostics.full_chunk_count,
                 talkback_diagnostics.discarded_residual_bytes_total,
                 talkback_connect_state == CONNECT_STATE_ONLINE ? "true" : "false",
                 talkback_authenticated ? "true" : "false",
                 talkback_client != NULL ? "true" : "false",
                 talkback_connect_state);
        if (attempt == 1)
            diagnostic_event("native_talkback_write_first_attempt", talkback_uid, extra);
        if (write_result && !talkback_diagnostics.first_success_reported) {
            diagnostic_event("native_talkback_write_first_success", talkback_uid, extra);
            talkback_diagnostics.first_success_reported = true;
        }
        if (!write_result && !talkback_diagnostics.first_failure_reported) {
            diagnostic_event("native_talkback_write_first_failure", talkback_uid, extra);
            talkback_diagnostics.first_failure_reported = true;
        }
    } else if (talkback_diagnostics.first_write_timing_count < 5) {
        char extra[256];
        snprintf(extra, sizeof(extra),
                 "channel=%d payload_size=640 timeout_ms=2000 write_result=%s "
                 "write_call_elapsed_ms=%lld since_previous_write_ms=%lld attempt=%llu",
                 TALKBACK_CHANNEL, write_result ? "true" : "false", call_elapsed,
                 since_previous, attempt);
        diagnostic_event("native_talkback_write_timing", talkback_uid, extra);
        talkback_diagnostics.first_write_timing_count++;
    }
    talkback_write_progress();
}

static void talkback_queue_reset(void) {
    pthread_mutex_lock(&talkback_queue.mutex);
    talkback_queue.head = 0;
    talkback_queue.tail = 0;
    talkback_queue.count = 0;
    talkback_queue.stop = false;
    pthread_mutex_unlock(&talkback_queue.mutex);
}

static void talkback_queue_stop(void) {
    pthread_mutex_lock(&talkback_queue.mutex);
    talkback_queue.stop = true;
    pthread_cond_broadcast(&talkback_queue.condition);
    pthread_mutex_unlock(&talkback_queue.mutex);
}

static size_t talkback_queue_depth(void) {
    size_t depth;
    pthread_mutex_lock(&talkback_queue.mutex);
    depth = talkback_queue.count;
    pthread_mutex_unlock(&talkback_queue.mutex);
    return depth;
}

static bool talkback_queue_push(const unsigned char *payload) {
    unsigned long long overflow_count = 0;
    pthread_mutex_lock(&talkback_queue.mutex);
    if (talkback_queue.stop || talkback_queue.count >= TALKBACK_QUEUE_CAPACITY) {
        overflow_count = ++talkback_diagnostics.pacing_overflow_count;
        pthread_mutex_unlock(&talkback_queue.mutex);
        if (overflow_count == 1 || overflow_count % 100 == 0) {
            char extra[160];
            snprintf(extra, sizeof(extra),
                     "queue_depth=%zu queue_capacity=%u overflow_count=%llu",
                     talkback_queue_depth(), TALKBACK_QUEUE_CAPACITY, overflow_count);
            diagnostic_event("native_talkback_pacing_overflow", talkback_uid, extra);
        }
        return false;
    }
    memcpy(talkback_queue.chunks[talkback_queue.tail].payload,
           payload, TALKBACK_CHUNK_BYTES);
    talkback_queue.tail = (talkback_queue.tail + 1) % TALKBACK_QUEUE_CAPACITY;
    talkback_queue.count++;
    if (talkback_queue.count > talkback_diagnostics.pacing_queue_depth_max)
        talkback_diagnostics.pacing_queue_depth_max = (unsigned int)talkback_queue.count;
    pthread_cond_signal(&talkback_queue.condition);
    pthread_mutex_unlock(&talkback_queue.mutex);
    return true;
}

static bool talkback_queue_pop(unsigned char *payload) {
    pthread_mutex_lock(&talkback_queue.mutex);
    while (talkback_queue.count == 0 && !talkback_queue.stop && stream_running)
        pthread_cond_wait(&talkback_queue.condition, &talkback_queue.mutex);
    if (talkback_queue.count == 0 || talkback_queue.stop || !stream_running) {
        pthread_mutex_unlock(&talkback_queue.mutex);
        return false;
    }
    memcpy(payload, talkback_queue.chunks[talkback_queue.head].payload,
           TALKBACK_CHUNK_BYTES);
    memset(talkback_queue.chunks[talkback_queue.head].payload, 0,
           TALKBACK_CHUNK_BYTES);
    talkback_queue.head = (talkback_queue.head + 1) % TALKBACK_QUEUE_CAPACITY;
    talkback_queue.count--;
    pthread_mutex_unlock(&talkback_queue.mutex);
    return true;
}

static void talkback_sleep_until(long long deadline_ms) {
    while (stream_running) {
        long long now = diagnostic_started_ms();
        if (now <= 0 || now >= deadline_ms) return;
        struct timespec remaining;
        remaining.tv_sec = (time_t)((deadline_ms - now) / 1000LL);
        remaining.tv_nsec = (long)(((deadline_ms - now) % 1000LL) * 1000000L);
        if (nanosleep(&remaining, NULL) != 0 && errno != EINTR) return;
    }
}

static void *talkback_pacer(void *unused) {
    (void)unused;
    unsigned char payload[TALKBACK_CHUNK_BYTES];
    unsigned long long paced_write_count = 0;
    unsigned long long interval_total = 0;
    unsigned long long interval_min = 0;
    unsigned long long interval_max = 0;
    unsigned long long late_write_count = 0;
    bool first = true;
    long long next_deadline = 0;
    diagnostic_event("native_talkback_pacer_started", talkback_uid,
                     "target_interval_ms=40 queue_capacity=16 chunk_bytes=640");
    while (talkback_queue_pop(payload)) {
        long long now = diagnostic_started_ms();
        if (first) {
            next_deadline = now > 0 ? now : 0;
            first = false;
            diagnostic_event("native_talkback_pacing_first", talkback_uid,
                             "target_interval_ms=40 queue_depth=0");
        }
        long long previous_write = talkback_diagnostics.previous_write_ms;
        talkback_sleep_until(next_deadline);
        long long before = diagnostic_started_ms();
        if (before > 0 && next_deadline > 0 && before > next_deadline)
            late_write_count++;
        talkback_write_one(payload);
        long long after = diagnostic_started_ms();
        if (talkback_diagnostics.write_attempt_count > 1 &&
            previous_write > 0 && before > 0) {
            long long interval = before - previous_write;
            if (interval >= 0) {
                interval_total += (unsigned long long)interval;
                if (interval_min == 0 || (unsigned long long)interval < interval_min)
                    interval_min = (unsigned long long)interval;
                if ((unsigned long long)interval > interval_max)
                    interval_max = (unsigned long long)interval;
            }
        }
        paced_write_count++;
        next_deadline += TALKBACK_PACING_INTERVAL_MS;
        if (after > 0 && next_deadline < after) next_deadline = after;
        if (paced_write_count % 250 == 0) {
            char extra[320];
            size_t depth = talkback_queue_depth();
            snprintf(extra, sizeof(extra),
                     "paced_write_count=%llu queue_depth=%zu queue_depth_max=%u "
                     "average_interval_ms=%.3f min_interval_ms=%llu max_interval_ms=%llu "
                     "late_write_count=%llu",
                     paced_write_count, depth, talkback_diagnostics.pacing_queue_depth_max,
                     interval_total > 0 && paced_write_count > 1
                         ? (double)interval_total / (double)(paced_write_count - 1) : 0.0,
                     interval_min, interval_max, late_write_count);
            diagnostic_event("native_talkback_pacing_progress", talkback_uid, extra);
        }
        memset(payload, 0, sizeof(payload));
    }
    return NULL;
}

static void *forward_talkback(void *unused) {
    (void)unused;
    unsigned char header[8];
    while (stream_running && talkback_fd >= 0 && talkback_write != NULL) {
        ssize_t got = read(talkback_fd, header, sizeof(header));
        if (got <= 0) break;
        if (got != (ssize_t)sizeof(header)) {
            talkback_diagnostics.short_header_read_count++;
            talkback_ipc_error("short_header_read",
                               talkback_diagnostics.short_header_read_count, got);
            continue;
        }
        if (memcmp(header, "OKT1", 4) != 0) {
            talkback_diagnostics.malformed_header_count++;
            talkback_ipc_error("malformed_header",
                               talkback_diagnostics.malformed_header_count, got);
            continue;
        }
        uint32_t length = ((uint32_t)header[4] << 24) | ((uint32_t)header[5] << 16) |
                          ((uint32_t)header[6] << 8) | header[7];
        if (length == 0 || length > 4096) {
            talkback_diagnostics.invalid_length_count++;
            talkback_ipc_error("invalid_declared_length",
                               talkback_diagnostics.invalid_length_count, (ssize_t)length);
            continue;
        }
        unsigned char *payload = malloc(length);
        if (payload == NULL) break;
        size_t offset = 0;
        while (offset < length) {
            ssize_t n = read(talkback_fd, payload + offset, length - offset);
            if (n <= 0) {
                talkback_diagnostics.incomplete_payload_count++;
                talkback_ipc_error("incomplete_payload",
                                   talkback_diagnostics.incomplete_payload_count, n);
                offset = 0;
                break;
            }
            offset += (size_t)n;
        }
        if (offset == length) {
            talkback_diagnostics.input_message_count++;
            talkback_diagnostics.input_message_bytes += length;
            size_t full_chunks = length / 640;
            size_t residual = length % 640;
            talkback_diagnostics.full_chunk_count += full_chunks;
            talkback_diagnostics.discarded_residual_bytes_total += residual;
            if (talkback_diagnostics.input_message_count == 1) {
                char extra[192];
                snprintf(extra, sizeof(extra),
                         "input_bytes=%u full_chunks=%zu chunk_bytes=640 residual_bytes=%zu",
                         length, full_chunks, residual);
                diagnostic_event("native_talkback_segmentation_first", talkback_uid, extra);
            }
            if (talkback_diagnostics.input_message_count % 250 == 0) {
                char extra[256];
                snprintf(extra, sizeof(extra),
                         "messages=%llu full_chunks=%llu residual_bytes_discarded=%llu",
                         talkback_diagnostics.input_message_count,
                         talkback_diagnostics.full_chunk_count,
                         talkback_diagnostics.discarded_residual_bytes_total);
                diagnostic_event("native_talkback_segmentation_progress", talkback_uid, extra);
            }
            for (size_t pos = 0; pos + TALKBACK_CHUNK_BYTES <= length; pos += TALKBACK_CHUNK_BYTES)
                (void)talkback_queue_push(payload + pos);
        }
        memset(payload, 0, length);
        free(payload);
    }
    return NULL;
}

static bool forward_h264_frames(client_read_fn client_read, void *client, const char *uid,
                                unsigned int *frames, unsigned long long *bytes,
                                bool *keyframe_seen, unsigned int *h265_frames) {
    unsigned long long packet_count = 0;
    unsigned int audio_frame_count = 0;
    unsigned long long audio_bytes_total = 0;
    unsigned int audio_pipe_frames = 0;
    unsigned long long audio_pipe_bytes = 0;
    unsigned int audio_pipe_errors = 0;
    while (stream_running) {
        time_t deadline = time(NULL) + STREAM_TIMEOUT_SECONDS;
        unsigned char header[VIDEO_HEADER_BYTES];
        if (!read_client_exact(client_read, client, VIDEO_CHANNEL,
                               header, sizeof(header), deadline)) return false;
        if (read_le32(header) != 0xa815aa55U) return false;
        uint32_t length = read_le32(header + 16);
        if (length == 0 || length > MAX_VIDEO_FRAME_BYTES) return false;
        unsigned char *payload = malloc(length);
        if (payload == NULL) return false;
        bool read_ok = read_client_exact(client_read, client, VIDEO_CHANNEL,
                                         payload, length, deadline);
        bool write_ok = true;
        if (read_ok) {
            packet_count++;
            if (packet_count == 1) {
                char extra[64];
                snprintf(extra, sizeof(extra), "bytes=%u", length);
                diagnostic_event("first_native_video_packet", uid, extra);
            }
            if (packet_count == 1 || packet_count % 100 == 0) {
                char extra[128];
                snprintf(extra, sizeof(extra), "native_video_packet_count=%llu bytes=%u",
                         packet_count, length);
                diagnostic_event("native_video_packet_progress", uid, extra);
            }
            if (header[4] == 0x0cU) {
                audio_frame_count++;
                audio_bytes_total += length;
                if (audio_frame_count == 1) {
                    char extra[96];
                    snprintf(extra, sizeof(extra), "frame_type=12 payload_bytes=%u channel=%u", length, VIDEO_CHANNEL);
                    diagnostic_event("native_audio_first_frame", uid, extra);
                } else if (audio_frame_count % 250 == 0) {
                    char extra[128];
                    snprintf(extra, sizeof(extra), "audio_frame_count=%u audio_bytes_total=%llu",
                             audio_frame_count, audio_bytes_total);
                    diagnostic_event("native_audio_progress", uid, extra);
                }
                int fd = atoi(getenv("OKAM_AUDIO_FD") != NULL ? getenv("OKAM_AUDIO_FD") : "-1");
                if (fd >= 0 && length <= 4096) {
                    unsigned char framed[8];
                    memcpy(framed, "OKA1", 4);
                    framed[4] = (unsigned char)(length >> 24); framed[5] = (unsigned char)(length >> 16);
                    framed[6] = (unsigned char)(length >> 8); framed[7] = (unsigned char)length;
                    ssize_t header_written = write(fd, framed, sizeof(framed));
                    ssize_t payload_written = write(fd, payload, length);
                    if (header_written == (ssize_t)sizeof(framed) && payload_written == (ssize_t)length) {
                        audio_pipe_frames++;
                        audio_pipe_bytes += length;
                        if (audio_pipe_frames == 1) {
                            char extra[128];
                            snprintf(extra, sizeof(extra), "frame_bytes=%u audio_pipe_frames=%u audio_pipe_bytes_total=%llu",
                                     length, audio_pipe_frames, audio_pipe_bytes);
                            diagnostic_event("audio_pipe_first_write", uid, extra);
                        } else if (audio_pipe_frames % 250 == 0) {
                            char extra[128];
                            snprintf(extra, sizeof(extra), "audio_pipe_frames=%u audio_pipe_bytes_total=%llu",
                                     audio_pipe_frames, audio_pipe_bytes);
                            diagnostic_event("audio_pipe_progress", uid, extra);
                        }
                    } else {
                        audio_pipe_errors++;
                        if (audio_pipe_errors == 1 || audio_pipe_errors % 250 == 0) {
                            char extra[128];
                            snprintf(extra, sizeof(extra), "error_count=%u frame_bytes=%u errno=%d",
                                     audio_pipe_errors, length, errno);
                            diagnostic_event(errno == EPIPE ? "audio_pipe_closed" : "audio_pipe_write_error", uid, extra);
                        }
                    }
                }
            } else if (header[4] == 0x10U || header[4] == 0x11U) {
                (*h265_frames)++;
            } else if (inspect_h264_payload(payload, length, keyframe_seen)) {
                diagnostic_h264_units(payload, length, uid);
                (*frames)++;
                *bytes += length;
                write_ok = write_stdout(payload, length);
                if (write_ok && (*frames == 1 || *frames % 100 == 0)) {
                    char extra[160];
                    snprintf(extra, sizeof(extra), "helper_stdout_bytes_total=%llu h264_frame_count=%u",
                             *bytes, *frames);
                    diagnostic_event("helper_stdout_progress", uid, extra);
                }
            }
        }
        memset(payload, 0, length);
        free(payload);
        if (!read_ok) return false;
        if (!write_ok) {
            stream_running = 0;
            break;
        }
    }
    return *frames > 0;
}

int main(int argc, char **argv) {
    const char *mode = argc >= 3 ? argv[2] : "";
    int credential_index = -1;
    if (argc == 5 && strcmp(argv[3], "--credential-index") == 0) {
        char *end = NULL;
        long parsed = strtol(argv[4], &end, 10);
        if (end == argv[4] || *end != '\0' || parsed < 0 || parsed > 255) {
            return 2;
        }
        credential_index = (int)parsed;
    } else if (argc != 2 && argc != 3) {
        return 2;
    }
    bool stream_test = strcmp(mode, "--stream-test") == 0;
    bool stream_stdout = strcmp(mode, "--stream-stdout") == 0;
    bool live_mode = stream_test || stream_stdout;
    bool authenticate = live_mode ||
        (strcmp(mode, "--authenticate") == 0);
    if (credential_index >= 0 && !authenticate) {
        return 2;
    }
    if ((argc != 2 && argc != 3 && argc != 5) || (!authenticate && argc != 2)) {
        fputs("usage: okam-hybris-connect /path/to/libOKSMARTPPCS.so "
              "[--authenticate|--stream-test|--stream-stdout] "
              "[--credential-index N]\n", stderr);
        return 2;
    }
    char *uid = read_field(false);
    char *service_parameter = read_field(false);
    char *device_password = authenticate ? read_field(true) : NULL;
    if (uid == NULL || service_parameter == NULL ||
        (authenticate && device_password == NULL) || !initialize_stack_guard()) {
        free(uid);
        free(service_parameter);
        free(device_password);
        fputs("invalid native P2P input\n", stderr);
        return 3;
    }
    hybris_set_hook_callback(okam_hook);
    void *library = android_dlopen(argv[1], RTLD_LAZY | RTLD_LOCAL);
    if (library == NULL) {
        free(uid);
        free(service_parameter);
        if (device_password != NULL) {
            memset(device_password, 0, strlen(device_password));
            free(device_password);
        }
        fputs("official native P2P library could not be loaded\n", stderr);
        return 3;
    }

    client_create_fn client_create = (client_create_fn)android_dlsym(library, "client_create");
    client_connect_fn client_connect = (client_connect_fn)android_dlsym(library, "client_connect");
    client_login_fn client_login = (client_login_fn)android_dlsym(library, "client_login");
    client_write_cgi_fn client_write_cgi =
        (client_write_cgi_fn)android_dlsym(library, "client_write_cgi");
    client_write_fn client_write = (client_write_fn)android_dlsym(library, "client_write");
    client_read_fn client_read = (client_read_fn)android_dlsym(library, "client_read");
    client_disconnect_fn client_disconnect =
        (client_disconnect_fn)android_dlsym(library, "client_disconnect");
    client_destroy_fn client_destroy = (client_destroy_fn)android_dlsym(library, "client_destroy");
    if (client_create == NULL || client_connect == NULL ||
        client_disconnect == NULL || client_destroy == NULL) {
        android_dlclose(library);
        free(uid);
        free(service_parameter);
        if (device_password != NULL) {
            memset(device_password, 0, strlen(device_password));
            free(device_password);
        }
        fputs("official native P2P lifecycle API is incomplete\n", stderr);
        return 3;
    }
    if (authenticate && (client_login == NULL || client_read == NULL)) {
        android_dlclose(library);
        free(uid);
        free(service_parameter);
        memset(device_password, 0, strlen(device_password));
        free(device_password);
        fputs("official native P2P authentication API is incomplete\n", stderr);
        return 3;
    }
    if (live_mode && client_write_cgi == NULL) {
        android_dlclose(library);
        free(uid);
        free(service_parameter);
        memset(device_password, 0, strlen(device_password));
        free(device_password);
        fputs("official native P2P live-stream API is incomplete\n", stderr);
        return 3;
    }

    void *client = client_create(uid, NULL);
    int state = -1;
    bool connected = false;
    bool disconnected = false;
    bool login_sent = false;
    bool login_response_received = false;
    bool authenticated = false;
    uint16_t login_command = 0;
    int login_result = -1;
    int login_candidate = -1;
    bool stream_start_sent = false;
    bool stream_stop_sent = false;
    bool h264_received = false;
    bool keyframe_seen = false;
    unsigned int h264_frames = 0;
    unsigned int h265_frames = 0;
    unsigned long long h264_bytes = 0;
    if (client != NULL) {
        state = client_connect(client, CONNECT_TYPE_NORMAL, service_parameter, 0);
        connected = state == CONNECT_STATE_ONLINE;
        if (connected && authenticate) {
            if (debug_credentials_enabled()) {
                print_debug_credential("ipc_read", uid, device_password);
                print_debug_credential("native_login_input", uid, device_password);
            } else {
                diagnostic_prefix();
                fprintf(stderr, "native_login_input username_present=true "
                                "password_present=true");
                if (device_password[0] == '\0') fprintf(stderr, " password_length=0");
                fputc('\n', stderr);
                fflush(stderr);
            }
            login_sent = client_login(client, "admin", device_password);
            if (login_sent) {
                login_response_received = await_login_response(
                    client_read, client, &login_command, &login_result);
                authenticated = login_response_received && login_result == 0;
                login_candidate = credential_index;
                {
                    char extra[192];
                    snprintf(extra, sizeof(extra),
                             "login_response_received=%s login_result=%d authenticated=%s",
                             login_response_received ? "true" : "false", login_result,
                             authenticated ? "true" : "false");
                    diagnostic_event("native_login_result", uid, extra);
                }
            }
        }
        {
            char extra[192];
            snprintf(extra, sizeof(extra),
                     "connect_state=%d connected=%s login_response_received=%s login_result=%d",
                     state, connected ? "true" : "false",
                     login_response_received ? "true" : "false", login_result);
            diagnostic_event("native_connect_result", uid, extra);
        }
        if (stream_stdout) {
            fprintf(stderr,
                    "{\"okam_auth\":true,\"connected\":%s,\"connect_state\":%d,"
                    "\"login_sent\":%s,\"login_response_received\":%s,"
                    "\"authenticated\":%s,\"login_command\":%u,\"login_result\":%d}\n",
                    connected ? "true" : "false", state,
                    login_sent ? "true" : "false",
                    login_response_received ? "true" : "false",
                    authenticated ? "true" : "false", login_command, login_result);
            fflush(stderr);
        }
        if (connected && authenticated && live_mode) {
            talkback_fd = atoi(getenv("OKAM_TALKBACK_FD") != NULL ? getenv("OKAM_TALKBACK_FD") : "-1");
            talkback_client = client;
            talkback_write = client_write;
            talkback_uid = uid;
            talkback_connect_state = state;
            talkback_authenticated = authenticated;
            memset(&talkback_diagnostics, 0, sizeof(talkback_diagnostics));
            talkback_started = false;
            talkback_pacer_started = false;
            talkback_queue_reset();
            {
                bool talkback_fd_valid = talkback_fd >= 0;
                bool client_write_resolved = talkback_write != NULL;
                int thread_result = -1;
                int pacer_thread_result = -1;
                if (talkback_fd_valid && client_write_resolved) {
                    pacer_thread_result = pthread_create(&talkback_pacer_thread, NULL, talkback_pacer, NULL);
                    talkback_pacer_started = pacer_thread_result == 0;
                    if (talkback_pacer_started) {
                        thread_result = pthread_create(&talkback_thread, NULL, forward_talkback, NULL);
                        talkback_started = thread_result == 0;
                    }
                }
                {
                    char extra[320];
                    snprintf(extra, sizeof(extra),
                             "talkback_fd=%d talkback_fd_valid=%s client_write_resolved=%s "
                             "connected=%s authenticated=%s live_mode=true pthread_result=%d "
                             "pacer_pthread_result=%d",
                             talkback_fd, talkback_fd_valid ? "true" : "false",
                             client_write_resolved ? "true" : "false",
                             connected ? "true" : "false",
                             authenticated ? "true" : "false", thread_result,
                             pacer_thread_result);
                    diagnostic_event(
                        talkback_started ? "native_talkback_thread_started"
                                         : "native_talkback_thread_start_failure",
                        talkback_uid, extra);
                }
            }
            diagnostic_event("livestream_command_begin", uid, "streamid=10 substream=2");
            stream_start_sent = client_write_cgi(
                client, "livestream.cgi?streamid=10&substream=2&", 5000);
            if (stream_start_sent) {
                diagnostic_event("audio_start_command_begin", uid, "command=audiostream.cgi?streamid=7&");
                bool audio_command_result = client_write_cgi(client, "audiostream.cgi?streamid=7&", 5000);
                diagnostic_event("audio_start_command_sent", uid, "status=sent");
                {
                    char extra[64];
                    snprintf(extra, sizeof(extra), "result=%s", audio_command_result ? "accepted" : "rejected");
                    diagnostic_event("audio_start_command_result", uid, extra);
                }
            }
            diagnostic_event("livestream_command_sent", uid,
                             stream_start_sent ? "stream_start_sent=true" : "stream_start_sent=false");
            if (stream_start_sent) {
                if (stream_stdout) {
                    signal(SIGPIPE, SIG_IGN);
                    signal(SIGINT, stop_streaming);
                    signal(SIGTERM, stop_streaming);
                    h264_received = forward_h264_frames(
                        client_read, client, uid, &h264_frames, &h264_bytes,
                        &keyframe_seen, &h265_frames);
                } else {
                    h264_received = await_h264_frames(
                        client_read, client, &h264_frames, &h264_bytes,
                        &keyframe_seen, &h265_frames);
                }
                stream_stop_sent = client_write_cgi(
                    client, "livestream.cgi?streamid=16&substream=0&", 5000);
                (void)client_write_cgi(
                    client, "audiostream.cgi?streamid=16&", 5000);
            }
        }
        if (connected) disconnected = client_disconnect(client);
        stream_running = 0;
        talkback_queue_stop();
        if (talkback_fd >= 0) close(talkback_fd);
        if (talkback_started) {
            (void)pthread_cancel(talkback_thread);
            (void)pthread_join(talkback_thread, NULL);
        }
        if (talkback_pacer_started) {
            (void)pthread_join(talkback_pacer_thread, NULL);
        }
        client_destroy(client);
    }
    memset(uid, 0, strlen(uid));
    free(uid);
    memset(service_parameter, 0, strlen(service_parameter));
    free(service_parameter);
    if (device_password != NULL) {
        memset(device_password, 0, strlen(device_password));
        free(device_password);
    }
    if (stream_stdout) {
        fprintf(stderr,
                "{\"connected\":%s,\"connect_state\":%d,\"login_sent\":%s,"
                "\"login_response_received\":%s,\"authenticated\":%s,"
                "\"login_command\":%u,\"login_result\":%d,"
                "\"login_candidate\":%d,"
                "\"stream_start_sent\":%s,\"stream_stop_sent\":%s,"
                "\"h264_received\":%s,\"h264_frames\":%u,\"h264_bytes\":%llu,"
                "\"keyframe_seen\":%s,\"h265_frames\":%u,\"disconnected\":%s}\n",
                connected ? "true" : "false", state, login_sent ? "true" : "false",
                login_response_received ? "true" : "false",
                authenticated ? "true" : "false", login_command, login_result,
                login_candidate,
                stream_start_sent ? "true" : "false", stream_stop_sent ? "true" : "false",
                h264_received ? "true" : "false", h264_frames, h264_bytes,
                keyframe_seen ? "true" : "false", h265_frames,
                disconnected ? "true" : "false");
    } else if (stream_test) {
        printf("{\"connected\":%s,\"connect_state\":%d,\"login_sent\":%s,"
               "\"login_response_received\":%s,\"authenticated\":%s,"
               "\"login_command\":%u,\"login_result\":%d,"
               "\"login_candidate\":%d,"
               "\"stream_start_sent\":%s,\"stream_stop_sent\":%s,"
               "\"h264_received\":%s,\"h264_frames\":%u,\"h264_bytes\":%llu,"
               "\"keyframe_seen\":%s,\"h265_frames\":%u,\"disconnected\":%s}\n",
               connected ? "true" : "false", state, login_sent ? "true" : "false",
               login_response_received ? "true" : "false",
               authenticated ? "true" : "false", login_command, login_result,
               login_candidate,
               stream_start_sent ? "true" : "false", stream_stop_sent ? "true" : "false",
               h264_received ? "true" : "false", h264_frames, h264_bytes,
               keyframe_seen ? "true" : "false", h265_frames,
               disconnected ? "true" : "false");
    } else if (authenticate && login_response_received) {
        printf("{\"connected\":%s,\"connect_state\":%d,\"login_sent\":%s,"
               "\"login_response_received\":true,\"authenticated\":%s,"
               "\"login_command\":%u,\"login_result\":%d,\"login_candidate\":%d,\"disconnected\":%s}\n",
               connected ? "true" : "false", state, login_sent ? "true" : "false",
               authenticated ? "true" : "false", login_command, login_result,
               login_candidate,
               disconnected ? "true" : "false");
    } else if (authenticate) {
        printf("{\"connected\":%s,\"connect_state\":%d,\"login_sent\":%s,"
               "\"login_response_received\":false,\"authenticated\":false,"
               "\"login_command\":null,\"login_result\":null,"
               "\"login_candidate\":%d,\"disconnected\":%s}\n",
               connected ? "true" : "false", state, login_sent ? "true" : "false",
               login_candidate,
               disconnected ? "true" : "false");
    } else {
        printf("{\"connected\":%s,\"connect_state\":%d,\"disconnected\":%s}\n",
               connected ? "true" : "false", state, disconnected ? "true" : "false");
    }
    android_dlclose(library);
    if (!connected || !disconnected) return 4;
    if (live_mode && (!authenticated || !stream_start_sent || !stream_stop_sent ||
                      !h264_received)) return 6;
    return !authenticate || authenticated ? 0 : 5;
}
