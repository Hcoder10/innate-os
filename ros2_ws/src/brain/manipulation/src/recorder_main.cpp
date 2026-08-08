#include <chrono>
#include <memory>
#include <rclcpp/rclcpp.hpp>
#include "manipulation/recorder_node.hpp"

int main(int argc, char** argv) {
    rclcpp::init(argc, argv);

    auto node = std::make_shared<manipulation::RecorderNode>();

    // Use MultiThreadedExecutor for concurrent callback handling.
    //
    // The 4th argument (next_exec_timeout) must be bounded. With the default
    // of -1, rcl_wait is handed an infinite timeout whenever the wait set
    // momentarily carries no active timer — and that wait happens while the
    // thread holds the executor's wait_mutex_. If no entity notification then
    // arrives, that thread never returns, every other executor thread piles up
    // on wait_mutex_, and the node stops serving callbacks entirely: no timer,
    // no subscriptions, and no services (not even rclcpp's own parameter
    // services). Recording could never start because
    // brain/recorder/activate_physical_primitive was never dispatched.
    // A finite timeout guarantees the wait always returns.
    rclcpp::executors::MultiThreadedExecutor executor(rclcpp::ExecutorOptions(), 0, false,
                                                      std::chrono::milliseconds(50));
    executor.add_node(node);

    RCLCPP_INFO(node->get_logger(), "Recorder Node (C++) starting with MultiThreadedExecutor");

    try {
        executor.spin();
    } catch (const std::exception& e) {
        RCLCPP_ERROR(node->get_logger(), "Exception: %s", e.what());
    }

    RCLCPP_INFO(node->get_logger(), "Recorder Node shutting down.");
    rclcpp::shutdown();
    return 0;
}
