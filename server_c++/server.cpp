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

#include <atomic>
#include <chrono>
#include <csignal>
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
#include <pty.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

// paho-mqtt-cpp
#include <mqtt/async_client.h>
#include <mqtt/callback.h>
#include <mqtt/connect_options.h>
#include <mqtt/message.h>

// ── JSON (single-header nlohmann/json) ────────────────────────────────────────
#include "json.hpp"
using json = nlohmann::json;

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

struct Session {
    std::string session_id;
    std::string device_id;
    int         master_fd  = -1;
    pid_t       pid        = -1;
    std::atomic<bool> alive{true};
    std::thread reader_thread;

    // topic helpers
    std::string topic_in()     const { return "shell/" + device_id + "/session/" + session_id + "/in"; }
    std::string topic_out()    const { return "shell/" + device_id + "/session/" + session_id + "/out"; }
    std::string topic_resize() const { return "shell/" + device_id + "/session/" + session_id + "/resize"; }

    ~Session() {
        alive = false;
        if (master_fd >= 0) ::close(master_fd);
        if (pid > 0) {
            ::kill(-pid, SIGTERM);
            int status;
            ::waitpid(pid, &status, WNOHANG);
        }
        if (reader_thread.joinable()) reader_thread.detach();
    }
};

// ── MoleServer ────────────────────────────────────────────────────────────────

class MoleServer : public virtual mqtt::callback,
                   public virtual mqtt::iaction_listener
{
public:
    MoleServer(const std::string& device_id,
               const std::string& broker_uri,
               const std::string& username,
               const std::string& password,
               const std::string& shell)
        : device_id_(device_id)
        , broker_uri_(broker_uri)
        , shell_(shell)
        , client_(broker_uri, "mole-server-" + device_id)
    {
        conn_opts_ = mqtt::connect_options_builder()
            .keep_alive_interval(std::chrono::seconds(60))
            .automatic_reconnect(true)
            .clean_session(true)
            .will(mqtt::message(
                "shell/" + device_id + "/presence",
                json{{"device_id", device_id}, {"online", false}}.dump(),
                1, true))
            .finalize();

        if (!username.empty())
            conn_opts_.set_user_name(username);
        if (!password.empty())
            conn_opts_.set_password(password);

        client_.set_callback(*this);
    }

    void run()
    {
        LOG_INFO("Connecting to broker " + broker_uri_ + " ...");
        try {
            client_.connect(conn_opts_)->wait();
        } catch (const mqtt::exception& e) {
            LOG_ERROR(std::string("Connection failed: ") + e.what());
            return;
        }

        // block until signal
        pause();

        LOG_INFO("Shutting down...");
        shutdown();
    }

    void shutdown()
    {
        std::lock_guard<std::mutex> lk(sessions_mutex_);
        for (auto& [id, sess] : sessions_)
            close_session(*sess);
        sessions_.clear();

        try { client_.disconnect()->wait(); } catch (...) {}
    }

private:
    // ── MQTT callbacks ────────────────────────────────────────────────────────

    void connected(const std::string&) override
    {
        LOG_INFO("Connected to broker " + broker_uri_);
        client_.subscribe("shell/" + device_id_ + "/control/new", 1);
        LOG_INFO("Listening on shell/" + device_id_ + "/control/new");
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
        if (topic == "shell/" + device_id_ + "/control/new") {
            try {
                auto j = json::parse(payload);
                std::string sid = j.value("session_id", random_id());
                create_session(sid);
            } catch (const std::exception& e) {
                LOG_ERROR(std::string("Bad session request: ") + e.what());
            }
            return;
        }

        // route to existing session
        std::lock_guard<std::mutex> lk(sessions_mutex_);
        for (auto& [id, sess] : sessions_) {
            if (topic == sess->topic_in()) {
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
        {
            std::lock_guard<std::mutex> lk(sessions_mutex_);
            if (sessions_.count(sid)) {
                LOG_WARN("Session " + sid + " already exists");
                return;
            }
        }

        LOG_INFO("Creating session " + sid);

        auto sess = std::make_unique<Session>();
        sess->session_id = sid;
        sess->device_id  = device_id_;

        // open PTY
        int slave_fd = -1;
        char slave_name[256];
        sess->master_fd = ::posix_openpt(O_RDWR | O_NOCTTY);
        if (sess->master_fd < 0) {
            LOG_ERROR("posix_openpt failed: " + std::string(strerror(errno)));
            return;
        }
        ::grantpt(sess->master_fd);
        ::unlockpt(sess->master_fd);
        slave_fd = ::open(::ptsname(sess->master_fd), O_RDWR);
        if (slave_fd < 0) {
            LOG_ERROR("open slave PTY failed: " + std::string(strerror(errno)));
            ::close(sess->master_fd);
            return;
        }
        LOG_DEBUG("PTY opened: master=" + std::to_string(sess->master_fd));

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
            ::close(sess->master_fd);

            // create new session and set controlling terminal
            ::setsid();
            ::ioctl(slave_fd, TIOCSCTTY, 0);

            ::dup2(slave_fd, STDIN_FILENO);
            ::dup2(slave_fd, STDOUT_FILENO);
            ::dup2(slave_fd, STDERR_FILENO);
            if (slave_fd > STDERR_FILENO) ::close(slave_fd);

            ::setenv("TERM", "xterm-256color", 1);
            ::setenv("MOLE_SESSION_ID", sid.c_str(), 1);

            ::execl(shell_.c_str(), shell_.c_str(), nullptr);
            ::_exit(127);  // exec failed
        }

        // ── parent ──
        ::close(slave_fd);
        LOG_DEBUG("Shell launched: PID " + std::to_string(sess->pid));

        // subscribe to session topics (outside lock)
        client_.subscribe(sess->topic_in(),     0);
        client_.subscribe(sess->topic_resize(), 0);

        // start PTY reader thread
        Session* sess_ptr = sess.get();
        sess->reader_thread = std::thread([this, sess_ptr]() {
            pty_reader_loop(sess_ptr);
        });

        // announce session (retained)
        json announce = {
            {"session_id", sid},
            {"device_id",  device_id_},
            {"shell",      shell_},
        };
        auto pub = mqtt::make_message(
            "shell/" + device_id_ + "/control/announce/" + sid,
            announce.dump(), 1, true);
        client_.publish(pub);
        LOG_DEBUG("Announce published for session " + sid);

        {
            std::lock_guard<std::mutex> lk(sessions_mutex_);
            sessions_[sid] = std::move(sess);
        }

        LOG_INFO("Session " + sid + " started (PID " + std::to_string(sess_ptr->pid) + ")");
        publish_presence();
    }

    void write_to_pty(Session& sess, const mqtt::binary& data)
    {
        if (!sess.alive) return;
        ssize_t n = ::write(sess.master_fd, data.data(), data.size());
        if (n < 0) {
            LOG_WARN("Write to PTY failed for session " + sess.session_id);
            close_session(sess);
        }
    }

    void resize_pty(Session& sess, int rows, int cols)
    {
        if (!sess.alive) return;
        struct winsize ws{};
        ws.ws_row = static_cast<unsigned short>(rows);
        ws.ws_col = static_cast<unsigned short>(cols);
        ::ioctl(sess.master_fd, TIOCSWINSZ, &ws);
        ::kill(-sess.pid, SIGWINCH);
        LOG_DEBUG("Session " + sess.session_id + " resized to "
                  + std::to_string(cols) + "x" + std::to_string(rows));
    }

    void pty_reader_loop(Session* sess)
    {
        char buf[4096];
        const std::string topic_out = sess->topic_out();

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
                if (::waitpid(sess->pid, &status, WNOHANG) > 0) break;
                continue;
            }

            ssize_t n = ::read(sess->master_fd, buf, sizeof(buf));
            if (n <= 0) break;

            auto msg = mqtt::make_message(topic_out,
                mqtt::binary(buf, buf + n), 0, false);
            try { client_.publish(msg); } catch (...) { break; }
        }

        close_session(*sess);
    }

    void close_session(Session& sess)
    {
        if (!sess.alive.exchange(false)) return;  // already closing

        LOG_INFO("Session " + sess.session_id + " closed");

        client_.unsubscribe(sess.topic_in());
        client_.unsubscribe(sess.topic_resize());

        // notify client
        auto msg = mqtt::make_message(
            sess.topic_out(),
            "\r\n[mole: session closed]\r\n",
            1, false);
        try { client_.publish(msg); } catch (...) {}

        // cleanup PTY and process
        if (sess.master_fd >= 0) {
            ::close(sess.master_fd);
            sess.master_fd = -1;
        }
        if (sess.pid > 0) {
            ::kill(-sess.pid, SIGTERM);
            int status;
            ::waitpid(sess.pid, &status, WNOHANG);
            sess.pid = -1;
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
        {
            std::lock_guard<std::mutex> lk(sessions_mutex_);
            for (auto& [id, s] : sessions_)
                if (s->alive) sessions_arr.push_back({{"session_id", id}});
        }

        json payload = {
            {"device_id",       device_id_},
            {"online",          true},
            {"shell",           shell_},
            {"active_sessions", sessions_arr.size()},
            {"sessions",        sessions_arr},
        };

        auto msg = mqtt::make_message(
            "shell/" + device_id_ + "/presence",
            payload.dump(), 1, true);
        try { client_.publish(msg); } catch (...) {}
    }

    // ── helpers ───────────────────────────────────────────────────────────────

    static std::string random_id()
    {
        return random_id_static();
    }

    static std::string random_id_static()
    {
        static const char chars[] = "0123456789abcdef";
        std::string id(8, ' ');
        for (auto& c : id) c = chars[rand() % 16];
        return id;
    }

    // ── members ───────────────────────────────────────────────────────────────

    std::string         device_id_;
    std::string         broker_uri_;
    std::string         shell_;
    mqtt::async_client  client_;
    mqtt::connect_options conn_opts_;

    std::mutex sessions_mutex_;
    std::map<std::string, std::unique_ptr<Session>> sessions_;
};

// ── signal handling ───────────────────────────────────────────────────────────

static MoleServer* g_server = nullptr;

static void signal_handler(int)
{
    // wake the main thread's pause()
    // shutdown() will be called in run() after pause() returns
}

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
};

static void print_usage(const char* prog)
{
    std::cout << "Usage: " << prog << " [OPTIONS]\n\n"
              << "Options:\n"
              << "  --broker     <host>   MQTT broker address   (default: localhost)\n"
              << "  --port       <port>   MQTT broker port      (default: 1883)\n"
              << "  --device-id  <id>     Device identifier     (default: hostname)\n"
              << "  --username   <user>   MQTT username\n"
              << "  --password   <pass>   MQTT password\n"
              << "  --shell      <shell>  Shell to expose       (default: /bin/bash)\n"
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
        cfg.device_id = "device-" + MoleServer::random_id_static();

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

    LOG_INFO("Device ID: " + cfg.device_id);

    // build broker URI
    std::string scheme = cfg.tls ? "ssl://" : "tcp://";
    std::string uri    = scheme + cfg.broker + ":" + std::to_string(cfg.port);

    MoleServer server(cfg.device_id, uri, cfg.username, cfg.password, cfg.shell);
    g_server = &server;

    // handle SIGINT / SIGTERM
    struct sigaction sa{};
    sa.sa_handler = signal_handler;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGINT,  &sa, nullptr);
    sigaction(SIGTERM, &sa, nullptr);

    server.run();
    return 0;
}
