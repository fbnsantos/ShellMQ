cmake_minimum_required(VERSION 3.14)
project(mole-server VERSION 1.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type")

# ── paho mqtt c++ ─────────────────────────────────────────────────────────────
find_package(PahoMqttCpp QUIET)
if(NOT PahoMqttCpp_FOUND)
    find_library(PAHO_CPP_LIB NAMES paho-mqttpp3 paho-mqtt-cpp
                 HINTS /usr/lib /usr/local/lib /usr/lib/aarch64-linux-gnu /usr/lib/arm-linux-gnueabihf)
    find_library(PAHO_C_LIB NAMES paho-mqtt3as paho-mqtt3a
                 HINTS /usr/lib /usr/local/lib /usr/lib/aarch64-linux-gnu /usr/lib/arm-linux-gnueabihf)
    find_path(PAHO_CPP_INC mqtt/async_client.h
              HINTS /usr/include /usr/local/include)
    if(NOT PAHO_CPP_LIB OR NOT PAHO_C_LIB OR NOT PAHO_CPP_INC)
        message(FATAL_ERROR
            "Paho MQTT C++ not found.\n"
            "Run: sudo apt install libpaho-mqtt-dev libpaho-mqttpp-dev")
    endif()
    message(STATUS "Paho MQTT C++: ${PAHO_CPP_LIB}")
endif()

# ── nlohmann json ─────────────────────────────────────────────────────────────
find_package(nlohmann_json QUIET)
if(NOT nlohmann_json_FOUND)
    find_path(JSON_INC nlohmann/json.hpp
              HINTS /usr/include /usr/local/include)
    if(NOT JSON_INC)
        message(FATAL_ERROR
            "nlohmann/json not found.\n"
            "Run: sudo apt install nlohmann-json3-dev")
    endif()
    message(STATUS "nlohmann/json: ${JSON_INC}")
endif()

# ── libuuid ───────────────────────────────────────────────────────────────────
find_library(UUID_LIB uuid HINTS /usr/lib /usr/local/lib
             /usr/lib/aarch64-linux-gnu /usr/lib/arm-linux-gnueabihf)
find_path(UUID_INC uuid/uuid.h HINTS /usr/include /usr/local/include)
if(NOT UUID_LIB OR NOT UUID_INC)
    message(FATAL_ERROR "libuuid not found.\nRun: sudo apt install uuid-dev")
endif()

# ── target ────────────────────────────────────────────────────────────────────
add_executable(mole-server server.cpp)

target_include_directories(mole-server PRIVATE
    $<$<NOT:$<BOOL:${PahoMqttCpp_FOUND}>>:${PAHO_CPP_INC}>
    $<$<NOT:$<BOOL:${nlohmann_json_FOUND}>>:${JSON_INC}>
    ${UUID_INC}
)

if(PahoMqttCpp_FOUND AND nlohmann_json_FOUND)
    target_link_libraries(mole-server PRIVATE
        PahoMqttCpp::paho-mqttpp3
        nlohmann_json::nlohmann_json
        ${UUID_LIB} pthread ssl crypto)
elseif(PahoMqttCpp_FOUND)
    target_link_libraries(mole-server PRIVATE
        PahoMqttCpp::paho-mqttpp3
        ${UUID_LIB} pthread ssl crypto)
else()
    target_link_libraries(mole-server PRIVATE
        ${PAHO_CPP_LIB} ${PAHO_C_LIB}
        ${UUID_LIB} pthread ssl crypto)
endif()

target_compile_options(mole-server PRIVATE -Wall -Wextra -O2)

install(TARGETS mole-server RUNTIME DESTINATION bin)

message(STATUS "")
message(STATUS "mole-server ${PROJECT_VERSION}  |  C++${CMAKE_CXX_STANDARD}  |  ${CMAKE_BUILD_TYPE}")
message(STATUS "")
