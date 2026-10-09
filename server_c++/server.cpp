/**
 * mole — server (C++ implementation)
 *
 * Exposes an interactive bash shell (with full PTY) over MQTT.
 * Drop-in replacement for server.py — compatible with the same
 * Python client and web client.
 *
 * Dependencies:
 *   - paho-mqtt-cpp  (libpaho-mqttpp-dev)
 *   - paho-mqtt-c    (libpaho-mqtt-dev)
 *
 * Build:
 *   cmake -B build && cmake --build build
 *
 * Usage:
 *   ./mole-server --broker localhost --device-id my-device
 */

#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

// POSIX
#include <fcntl.h>
#include <pthread.h>
#include <sys/ioctl.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

// paho-mqtt-cpp
#include <mqtt/async_client.h>
#include <mqtt/callback.h>
#include <mqtt/connect_options.h>
#include <mqtt/message.h>

// ── JSON (nlohmann/json) ──────────────────────────────────────────────────────
#include <nlohmann/json.hpp>
using json = nlohmann::json;

extern char** environ;

// recent output kept per session, replayed to clients that reattach
static constexpr size_t SCROLLBACK_BYTES = 64 * 1024;

static int64_t now_seconds()
{
    return std::chrono::duration_cast<std::chrono::seconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

// attach reply ids become part of a topic: no wildcards or separators
static bool valid_reply_id(const std::string& id)
{
    if (id.empty() || id.size() > 32) return false;
    return std::all_of(id.begin(), id.end(), [](char c) {
        return isalnum(static_cast<unsigned char>(c)) || c == '_' || c == '-';
    });
}

// ── logging ───────────────────────────────────────────────────────────────────

static std::mutex g_log_mutex;

enum class LogLevel { INFO, DEBUG, WARNING, ERROR_ };

static bool g_debug = false;

static void log(LogLevel level, const std::string& msg)
{
    if (level == LogLevel::DEBUG && !g_debug) return;

    // timestamp HH:MM:SS
    auto now  = std::chrono::system_clock::now();
    auto tt   = std::chrono::system_clock::to_time_t(now);
    struct tm tm_buf;
    localtime_r(&tt, &tm_buf);
    char ts[16];
    strftime(ts, sizeof(ts), "%H:%M:%S", &tm_buf);

    const char* label = "INFO";
    const char* color = "\033[0m";
    switch (level) {
        case LogLevel::DEBUG:   label = "DEBUG";   color = "\033[90m"; break;
        case LogLevel::WARNING: label = "WARNING";  color = "\033[33m"; break;
        case LogLevel::ERROR_:  label = "ERROR";    color = "\033[31m"; break;
        default:                label = "INFO";     color = "\033[36m"; break;
    }

    std::lock_guard<std::mutex> lk(g_log_mutex);
    std::cerr << ts << " \033[1m[SERVER]\033[0m "
              << color << label << "\033[0m " << msg << "\n";
}

#define LOG_INFO(msg)  log(LogLevel::INFO,    msg)
#define LOG_DEBUG(msg) log(LogLevel::DEBUG,   msg)
#define LOG_WARN(msg)  log(LogLevel::WARNING, msg)
#define LOG_ERROR(msg) log(LogLevel::ERROR_,  msg)

// ── Session ───────────────────────────────────────────────────────────────────
//
// Sessions are shared_ptr: the map, the MQTT callback and the PTY reader
// thread may all hold one, so a session is never freed while in use.
// Only the reader thread closes master_fd and reaps the shell.

struct Session {
    std::string session_id;
    std::string device_id;
    int         master_fd  = -1;
    pid_t       pid        = -1;
    std::atomic<bool> alive{true};
    std::mutex  fd_mutex;           // guards master_fd against the reader closing it
    double      created_at = 0;     // unix time, shown to clients
    std::atomic<int64_t> last_seen{now_seconds()};  // last input/attach/keepalive

    // held while appending + publishing output, so a replay snapshot never
    // misses or duplicates a chunk
    std::mutex  out_mutex;
    std::string scrollback;

    // topic helpers
    std::string topic_in()       const { return "shell/" + device_id + "/session/" + session_id + "/in"; }
    std::string topic_out()      const { return "shell/" + device_id + "/session/" + session_id + "/out"; }
    std::string topic_resize()   const { return "shell/" + device_id + "/session/" + session_id + "/resize"; }
    std::string topic_announce() const { return "shell/" + device_id + "/control/announce/" + session_id; }
    std::string topic_replay(const std::string& reply_id) const {
        return "shell/" + device_id + "/session/" + session_id + "/replay/" + reply_id;
    }
};

using SessionPtr = std::shared_ptr<Session>;

// ── MoleServer ────────────────────────────────────────────────────────────────

class MoleServer : public virtual mqtt::callback,
                   public virtual mqtt::iaction_listener
{
public:
    MoleServer(const std::string& device_id,
               const std::string& broker_uri,
               const std::string& username,
               const std::string& password,
               const std::string& shell,
               bool tls,
               int session_timeout)
        : device_id_(device_id)
        , broker_uri_(broker_uri)
        , shell_(shell)
        , session_timeout_(session_timeout)
        , client_(broker_uri, "mole-server-" + device_id)
    {
        auto builder = mqtt::connect_options_builder()
            .keep_alive_interval(std::chrono::seconds(60))
            .automatic_reconnect(true)
            .clean_session(true)
            .will(mqtt::message(
                topic_presence(),
                json{{"device_id", device_id}, {"online", false}}.dump(),
                1, true));
        if (tls)
            builder.ssl(mqtt::ssl_options());   // system CA store
        conn_opts_ = builder.finalize();

        if (!username.empty())
            conn_opts_.set_user_name(username);
        if (!password.empty())
            conn_opts_.set_password(password);

        client_.set_callback(*this);
    }

    // Blocks until one of the signals in `sigs` arrives, then shuts down.
    // The signals must already be blocked in every thread.
    void run(const sigset_t& sigs)
    {
        LOG_INFO("Connecting to broker " + broker_uri_ + " ...");
        try {
            client_.connect(conn_opts_)->wait();
        } catch (const mqtt::exception& e) {
            LOG_ERROR(std::string("Connection failed: ") + e.what());
            return;
        }

        if (session_timeout_ > 0)
            reaper_ = std::thread([this] { reap_idle_sessions(); });

        int sig = 0;
        sigwait(&sigs, &sig);

        LOG_INFO(std::string("Received ") + strsignal(sig) + ", shutting down...");
        shutdown();
    }

    void shutdown()
    {
        {
            std::lock_guard<std::mutex> lk(reaper_mutex_);
            stopping_ = true;
        }
        reaper_cv_.notify_all();
        if (reaper_.joinable()) reaper_.join();

        for (auto& sess : snapshot_sessions())
            request_close(*sess);

        // wait for the reader threads to publish their close messages
        {
            std::unique_lock<std::mutex> lk(readers_mutex_);
            if (!readers_cv_.wait_for(lk, std::chrono::seconds(5),
                                      [this] { return active_readers_ == 0; }))
                LOG_WARN("Some sessions did not close in time");
        }

        // a clean disconnect does not trigger the Last Will, mark offline ourselves
        try {
            client_.publish(mqtt::make_message(
                topic_presence(),
                json{{"device_id", device_id_}, {"online", false}}.dump(),
                1, true))->wait_for(std::chrono::seconds(2));
        } catch (...) {}

        try { client_.disconnect()->wait(); } catch (...) {}
    }

    static std::string random_id()
    {
        static const char chars[] = "0123456789abcdef";
        std::string id(8, ' ');
        for (auto& c : id) c = chars[rand() % 16];
        return id;
    }

private:
    std::string topic_presence() const { return "shell/" + device_id_ + "/presence"; }
    std::string topic_new()      const { return "shell/" + device_id_ + "/control/new"; }
    std::string topic_close()    const { return "shell/" + device_id_ + "/control/close"; }
    std::string topic_attach()   const { return "shell/" + device_id_ + "/control/attach"; }
    std::string topic_keepalive() const { return "shell/" + device_id_ + "/control/keepalive"; }

    std::vector<SessionPtr> snapshot_sessions()
    {
        std::lock_guard<std::mutex> lk(sessions_mutex_);
        std::vector<SessionPtr> out;
        for (auto& [id, s] : sessions_) out.push_back(s);
        return out;
    }

    SessionPtr find_session(const std::string& sid)
    {
        std::lock_guard<std::mutex> lk(sessions_mutex_);
        auto it = sessions_.find(sid);
        return it == sessions_.end() ? nullptr : it->second;
    }

    // ── MQTT callbacks ────────────────────────────────────────────────────────

    // Called on the first connection and after every automatic reconnect.
    void connected(const std::string&) override
    {
        LOG_INFO("Connected to broker " + broker_uri_);
        try {
            client_.subscribe(topic_new(),   1);
            client_.subscribe(topic_close(), 1);
            client_.subscribe(topic_attach(), 1);
            client_.subscribe(topic_keepalive(), 0);
            LOG_INFO("Listening on " + topic_new());

            // clean session: subscriptions are lost on reconnect, restore them
            // for the sessions that are still open
            auto open_sessions = snapshot_sessions();
            for (auto& sess : open_sessions) {
                client_.subscribe(sess->topic_in(),     0);
                client_.subscribe(sess->topic_resize(), 0);
            }
            if (!open_sessions.empty())
                LOG_INFO("Resubscribed " + std::to_string(open_sessions.size())
                         + " open session(s)");
        } catch (const mqtt::exception& e) {
            LOG_ERROR(std::string("Subscribe failed: ") + e.what());
        }
        publish_presence();
    }

    void connection_lost(const std::string& cause) override
    {
        LOG_WARN("Connection lost: " + cause + " — reconnecting...");
    }

    void message_arrived(mqtt::const_message_ptr msg) override
    {
        const std::string& topic   = msg->get_topic();
        const std::string  payload = msg->to_string();

        // new session request
        if (topic == topic_new()) {
            try {
                auto j = json::parse(payload);
                std::string sid = j.value("session_id", random_id());
                create_session(sid);
            } catch (const std::exception& e) {
                LOG_ERROR(std::string("Bad session request: ") + e.what());
            }
            return;
        }

        // close request
        if (topic == topic_close()) {
            try {
                auto j = json::parse(payload);
                if (auto sess = find_session(j.value("session_id", ""))) {
                    LOG_INFO("Close requested for session " + sess->session_id);
                    request_close(*sess);
                }
            } catch (const std::exception& e) {
                LOG_ERROR(std::string("Bad close request: ") + e.what());
            }
            return;
        }

        // client wants to reattach: send it the recent output
        if (topic == topic_attach()) {
            try {
                auto j = json::parse(payload);
                std::string reply_id = j.value("reply_id", "");
                if (!valid_reply_id(reply_id)) {
                    LOG_WARN("Attach request with invalid reply_id ignored");
                    return;
                }
                auto sess = find_session(j.value("session_id", ""));
                if (sess && sess->alive) {
                    LOG_INFO("Client attached to session " + sess->session_id);
                    sess->last_seen = now_seconds();
                    std::lock_guard<std::mutex> lk(sess->out_mutex);
                    client_.publish(mqtt::make_message(sess->topic_replay(reply_id),
                                                       sess->scrollback, 1, false));
                }
            } catch (const std::exception& e) {
                LOG_ERROR(std::string("Bad attach request: ") + e.what());
            }
            return;
        }

        // attached clients say they are still there
        if (topic == topic_keepalive()) {
            try {
                auto j = json::parse(payload);
                auto ids = j.value("session_ids", json::array());
                for (auto& id : ids)
                    if (id.is_string())
                        if (auto sess = find_session(id.get<std::string>()))
                            sess->last_seen = now_seconds();
            } catch (...) {}
            return;
        }

        // route to existing session (lock released before touching the PTY)
        for (auto& sess : snapshot_sessions()) {
            if (topic == sess->topic_in()) {
                sess->last_seen = now_seconds();
                write_to_pty(*sess, msg->get_payload());
                return;
            }
            if (topic == sess->topic_resize()) {
                try {
                    auto j = json::parse(payload);
                    resize_pty(*sess, j.value("rows", 24), j.value("cols", 80));
                } catch (...) {}
                return;
            }
        }
    }

    void on_failure(const mqtt::token& tok) override
    {
        LOG_ERROR("MQTT action failed: rc=" + std::to_string(tok.get_return_code()));
    }

    void on_success(const mqtt::token&) override {}

    // ── session management ────────────────────────────────────────────────────

    void create_session(const std::string& sid)
    {
        if (find_session(sid)) {
            LOG_WARN("Session " + sid + " already exists");
            return;
        }

        LOG_INFO("Creating session " + sid);

        auto sess = std::make_shared<Session>();
        sess->session_id = sid;
        sess->device_id  = device_id_;
        sess->created_at = std::chrono::duration<double>(
            std::chrono::system_clock::now().time_since_epoch()).count();

        // open PTY; O_CLOEXEC so other sessions' shells do not inherit this master
        sess->master_fd = ::posix_openpt(O_RDWR | O_NOCTTY | O_CLOEXEC);
        if (sess->master_fd < 0) {
            LOG_ERROR("posix_openpt failed: " + std::string(strerror(errno)));
            return;
        }
        ::grantpt(sess->master_fd);
        ::unlockpt(sess->master_fd);
        // O_NOCTTY: without it the pty becomes the server's controlling terminal
        // when running under systemd, and its hangup would SIGHUP the server
        int slave_fd = ::open(::ptsname(sess->master_fd), O_RDWR | O_NOCTTY);
        if (slave_fd < 0) {
            LOG_ERROR("open slave PTY failed: " + std::string(strerror(errno)));
            ::close(sess->master_fd);
            return;
        }
        LOG_DEBUG("PTY opened: master=" + std::to_string(sess->master_fd));

        // everything the child needs is prepared before fork(): after fork only
        // async-signal-safe calls are allowed in a multithreaded process
        std::vector<std::string> env_strs;
        for (char** e = environ; *e; ++e) {
            if (strncmp(*e, "TERM=", 5) == 0 || strncmp(*e, "MOLE_", 5) == 0) continue;
            env_strs.emplace_back(*e);
        }
        env_strs.emplace_back("TERM=xterm-256color");
        env_strs.emplace_back("MOLE_SESSION_ID=" + sid);
        std::vector<char*> envp;
        for (auto& s : env_strs) envp.push_back(s.data());
        envp.push_back(nullptr);
        char* argv[] = { const_cast<char*>(shell_.c_str()), nullptr };

        struct rlimit rl{};
        int max_fd = (::getrlimit(RLIMIT_NOFILE, &rl) == 0 && rl.rlim_cur != RLIM_INFINITY)
                     ? static_cast<int>(rl.rlim_cur) : 1024;

        // fork
        sess->pid = ::fork();
        if (sess->pid < 0) {
            LOG_ERROR("fork failed: " + std::string(strerror(errno)));
            ::close(sess->master_fd);
            ::close(slave_fd);
            return;
        }

        if (sess->pid == 0) {
            // ── child ──
            // the server blocks/ignores signals; give the shell the defaults back
            sigset_t none;
            sigemptyset(&none);
            ::sigprocmask(SIG_SETMASK, &none, nullptr);
            ::signal(SIGPIPE, SIG_DFL);

            // create new session and set controlling terminal
            ::setsid();
            ::ioctl(slave_fd, TIOCSCTTY, 0);

            ::dup2(slave_fd, STDIN_FILENO);
            ::dup2(slave_fd, STDOUT_FILENO);
            ::dup2(slave_fd, STDERR_FILENO);

            // do not leak the broker socket or other sessions' fds into the shell
            for (int fd = STDERR_FILENO + 1; fd < max_fd; ++fd) ::close(fd);

            ::execve(argv[0], argv, envp.data());
            ::_exit(127);  // exec failed
        }

        // ── parent ──
        ::close(slave_fd);
        LOG_DEBUG("Shell launched: PID " + std::to_string(sess->pid));

        {
            std::lock_guard<std::mutex> lk(sessions_mutex_);
            sessions_[sid] = sess;
        }

        // subscribe to session topics
        try {
            client_.subscribe(sess->topic_in(),     0);
            client_.subscribe(sess->topic_resize(), 0);
        } catch (const mqtt::exception& e) {
            LOG_ERROR(std::string("Subscribe failed: ") + e.what());
        }

        // start PTY reader thread; it owns the session's cleanup
        {
            std::lock_guard<std::mutex> lk(readers_mutex_);
            ++active_readers_;
        }
        std::thread([this, sess]() {
            pty_reader_loop(sess);
            {
                std::lock_guard<std::mutex> lk(readers_mutex_);
                --active_readers_;
            }
            readers_cv_.notify_all();
        }).detach();

        // announce session (retained, cleared when the session closes)
        json announce = {
            {"session_id", sid},
            {"device_id",  device_id_},
            {"shell",      shell_},
            {"created_at", sess->created_at},
        };
        try {
            client_.publish(mqtt::make_message(sess->topic_announce(),
                                               announce.dump(), 1, true));
        } catch (...) {}
        LOG_DEBUG("Announce published for session " + sid);

        LOG_INFO("Session " + sid + " started (PID " + std::to_string(sess->pid) + ")");
        publish_presence();
    }

    void write_to_pty(Session& sess, const mqtt::binary& data)
    {
        std::lock_guard<std::mutex> lk(sess.fd_mutex);
        if (!sess.alive || sess.master_fd < 0) return;

        size_t off = 0;
        while (off < data.size()) {
            ssize_t n = ::write(sess.master_fd, data.data() + off, data.size() - off);
            if (n < 0) {
                if (errno == EINTR) continue;
                LOG_WARN("Write to PTY failed for session " + sess.session_id
                         + ": " + strerror(errno));
                request_close(sess);
                return;
            }
            off += static_cast<size_t>(n);
        }
    }

    void resize_pty(Session& sess, int rows, int cols)
    {
        std::lock_guard<std::mutex> lk(sess.fd_mutex);
        if (!sess.alive || sess.master_fd < 0) return;
        struct winsize ws{};
        ws.ws_row = static_cast<unsigned short>(rows);
        ws.ws_col = static_cast<unsigned short>(cols);
        ::ioctl(sess.master_fd, TIOCSWINSZ, &ws);
        ::kill(-sess.pid, SIGWINCH);
        LOG_DEBUG("Session " + sess.session_id + " resized to "
                  + std::to_string(cols) + "x" + std::to_string(rows));
    }

    void pty_reader_loop(const SessionPtr& sess)
    {
        char buf[4096];
        const std::string topic_out = sess->topic_out();
        bool reaped = false;

        while (sess->alive) {
            fd_set rfds;
            FD_ZERO(&rfds);
            FD_SET(sess->master_fd, &rfds);
            struct timeval tv{0, 100000};  // 100ms

            int r = ::select(sess->master_fd + 1, &rfds, nullptr, nullptr, &tv);
            if (r < 0) {
                if (errno == EINTR) continue;
                break;
            }
            if (r == 0) {
                // check if child has exited
                int status;
                if (::waitpid(sess->pid, &status, WNOHANG) > 0) { reaped = true; break; }
                continue;
            }

            ssize_t n = ::read(sess->master_fd, buf, sizeof(buf));
            if (n <= 0) break;

            std::lock_guard<std::mutex> lk(sess->out_mutex);
            sess->scrollback.append(buf, static_cast<size_t>(n));
            if (sess->scrollback.size() > SCROLLBACK_BYTES)
                sess->scrollback.erase(0, sess->scrollback.size() - SCROLLBACK_BYTES);
            auto msg = mqtt::make_message(topic_out,
                mqtt::binary(buf, buf + n), 0, false);
            try { client_.publish(msg); } catch (...) {}  // dropped while offline
        }

        finish_session(*sess, reaped);
    }

    // Closes sessions no client has touched for session_timeout_ seconds
    // (tab closed, client crashed...), so shells do not pile up.
    void reap_idle_sessions()
    {
        std::unique_lock<std::mutex> lk(reaper_mutex_);
        while (!reaper_cv_.wait_for(lk, std::chrono::seconds(30), [this] { return stopping_; })) {
            int64_t now = now_seconds();
            for (auto& sess : snapshot_sessions()) {
                if (sess->alive && now - sess->last_seen > session_timeout_) {
                    LOG_INFO("Session " + sess->session_id + " idle for more than "
                             + std::to_string(session_timeout_) + " s without clients, closing");
                    request_close(*sess);
                }
            }
        }
    }

    // Asks a session to close. Safe from any thread; the reader thread
    // notices within 100 ms and runs finish_session().
    void request_close(Session& sess)
    {
        if (!sess.alive.exchange(false)) return;  // already closing
        ::kill(-sess.pid, SIGHUP);                // interactive bash ignores SIGTERM
    }

    // Runs only on the session's reader thread.
    void finish_session(Session& sess, bool reaped)
    {
        sess.alive = false;
        LOG_INFO("Session " + sess.session_id + " closed");

        try {
            client_.unsubscribe(sess.topic_in());
            client_.unsubscribe(sess.topic_resize());

            // notify client
            client_.publish(mqtt::make_message(
                sess.topic_out(), "\r\n[mole: session closed]\r\n", 1, false));
            // clear the retained announce so it does not stay in the broker forever
            client_.publish(mqtt::make_message(sess.topic_announce(), "", 1, true));
        } catch (...) {}

        // closing the master also hangs up the shell
        {
            std::lock_guard<std::mutex> lk(sess.fd_mutex);
            ::close(sess.master_fd);
            sess.master_fd = -1;
        }

        if (!reaped) {
            ::kill(-sess.pid, SIGHUP);
            int status;
            bool exited = false;
            for (int i = 0; i < 20 && !exited; ++i) {   // up to 2 s
                if (::waitpid(sess.pid, &status, WNOHANG) != 0) exited = true;
                else std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }
            if (!exited) {
                LOG_WARN("Shell of session " + sess.session_id + " ignored SIGHUP, killing");
                ::kill(-sess.pid, SIGKILL);
                ::waitpid(sess.pid, &status, 0);
            }
        }

        {
            std::lock_guard<std::mutex> lk(sessions_mutex_);
            sessions_.erase(sess.session_id);
        }

        publish_presence();
    }

    // ── presence ──────────────────────────────────────────────────────────────

    void publish_presence()
    {
        json sessions_arr = json::array();
        for (auto& s : snapshot_sessions())
            if (s->alive) sessions_arr.push_back({{"session_id", s->session_id},
                                                  {"created_at", s->created_at}});

        json payload = {
            {"device_id",       device_id_},
            {"online",          true},
            {"shell",           shell_},
            {"active_sessions", sessions_arr.size()},
            {"sessions",        sessions_arr},
        };

        auto msg = mqtt::make_message(topic_presence(), payload.dump(), 1, true);
        try { client_.publish(msg); } catch (...) {}
    }

    // ── members ───────────────────────────────────────────────────────────────

    std::string         device_id_;
    std::string         broker_uri_;
    std::string         shell_;
    int                 session_timeout_;
    mqtt::async_client  client_;
    mqtt::connect_options conn_opts_;

    std::mutex sessions_mutex_;
    std::map<std::string, SessionPtr> sessions_;

    std::mutex              readers_mutex_;
    std::condition_variable readers_cv_;
    int                     active_readers_ = 0;

    std::thread             reaper_;
    std::mutex              reaper_mutex_;
    std::condition_variable reaper_cv_;
    bool                    stopping_ = false;
};

// ── argument parsing ──────────────────────────────────────────────────────────

struct Config {
    std::string broker    = "localhost";
    int         port      = 1883;
    std::string device_id;
    std::string username;
    std::string password;
    std::string shell     = "/bin/bash";
    bool        tls       = false;
    bool        debug     = false;
    int         session_timeout = 600;
};

static void print_usage(const char* prog)
{
    std::cout << "Usage: " << prog << " [OPTIONS]\n\n"
              << "Options:\n"
              << "  --broker     <host>   MQTT broker address   (default: localhost)\n"
              << "  --port       <port>   MQTT broker port      (default: 1883)\n"
              << "  --device-id  <id>     Device identifier     (default: hostname)\n"
              << "  --username   <user>   MQTT username         (default: $MOLE_USERNAME)\n"
              << "  --password   <pass>   MQTT password         (default: $MOLE_PASSWORD;\n"
              << "                        prefer the variable, arguments are visible in ps)\n"
              << "  --shell      <shell>  Shell to expose       (default: /bin/bash)\n"
              << "  --session-timeout <s> Close sessions with no attached client for\n"
              << "                        this many seconds (default: 600, 0 = never)\n"
              << "  --tls                 Enable TLS\n"
              << "  --debug               Enable debug logging\n"
              << "  --help                Show this help\n";
}

static Config parse_args(int argc, char** argv)
{
    Config cfg;

    // default device-id = hostname
    char hostname[256];
    if (::gethostname(hostname, sizeof(hostname)) == 0)
        cfg.device_id = hostname;
    else
        cfg.device_id = "device-" + MoleServer::random_id();

    if (const char* u = std::getenv("MOLE_USERNAME")) cfg.username = u;
    if (const char* p = std::getenv("MOLE_PASSWORD")) cfg.password = p;

    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        auto next = [&]() -> std::string {
            if (i + 1 >= argc) {
                std::cerr << "Missing value for " << arg << "\n";
                exit(1);
            }
            return argv[++i];
        };

        if      (arg == "--broker")    cfg.broker    = next();
        else if (arg == "--port")      cfg.port      = std::stoi(next());
        else if (arg == "--device-id") cfg.device_id = next();
        else if (arg == "--username")  cfg.username  = next();
        else if (arg == "--password")  cfg.password  = next();
        else if (arg == "--shell")     cfg.shell     = next();
        else if (arg == "--session-timeout") cfg.session_timeout = std::stoi(next());
        else if (arg == "--tls")       cfg.tls       = true;
        else if (arg == "--debug")     cfg.debug     = true;
        else if (arg == "--help")      { print_usage(argv[0]); exit(0); }
        else { std::cerr << "Unknown option: " << arg << "\n"; exit(1); }
    }

    if (cfg.tls && cfg.port == 1883) cfg.port = 8883;

    return cfg;
}

// ── main ──────────────────────────────────────────────────────────────────────

int main(int argc, char** argv)
{
    srand(static_cast<unsigned>(time(nullptr)));

    Config cfg = parse_args(argc, argv);
    g_debug = cfg.debug;

    // Block the shutdown signals before any thread exists (paho creates its own),
    // so they are only delivered to sigwait() in MoleServer::run().
    sigset_t sigs;
    sigemptyset(&sigs);
    sigaddset(&sigs, SIGINT);
    sigaddset(&sigs, SIGTERM);
    sigaddset(&sigs, SIGHUP);
    pthread_sigmask(SIG_BLOCK, &sigs, nullptr);
    ::signal(SIGPIPE, SIG_IGN);

    LOG_INFO("Device ID: " + cfg.device_id);

    // build broker URI
    std::string scheme = cfg.tls ? "ssl://" : "tcp://";
    std::string uri    = scheme + cfg.broker + ":" + std::to_string(cfg.port);

    MoleServer server(cfg.device_id, uri, cfg.username, cfg.password, cfg.shell, cfg.tls,
                      cfg.session_timeout);
    server.run(sigs);
    return 0;
}
