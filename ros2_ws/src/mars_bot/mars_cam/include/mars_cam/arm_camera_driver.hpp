#pragma once

#include <rclcpp/rclcpp.hpp>
#include <rclcpp_components/register_node_macro.hpp>
#include <sensor_msgs/msg/image.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>
#include <opencv2/opencv.hpp>
#include <thread>
#include <atomic>
#include <mutex>

namespace mars_cam {

/**
 * @brief GStreamer-based arm camera driver node for Mars robot
 *
 * This node uses GStreamer with YUYV format to capture frames from the
 * arm-mounted Arducam USB camera and publishes both raw and compressed images.
 *
 * GStreamer provides robust device handling and hardware-accelerated
 * color conversion on Jetson platforms.
 */
class ArmCameraDriver : public rclcpp::Node {
   public:
    /**
     * @brief Constructor
     * @param options Node options for component composition
     */
    explicit ArmCameraDriver(const rclcpp::NodeOptions& options = rclcpp::NodeOptions());

    /**
     * @brief Destructor
     */
    ~ArmCameraDriver();

   private:
    /**
     * @brief Initialize camera with GStreamer pipeline
     * @return true if successful, false otherwise
     */
    bool initializeCamera();

    /**
     * @brief Create GStreamer pipeline string for YUYV capture
     * @return GStreamer pipeline string
     */
    std::string createGStreamerPipeline();

    /**
     * @brief Frame processing loop (runs in separate thread)
     */
    void frameProcessingLoop();

    /**
     * @brief Process and publish a captured frame
     * @param frame The captured frame
     */
    void processAndPublishFrame(const cv::Mat& frame);

    // ROS 2 publishers
    rclcpp::Publisher<sensor_msgs::msg::Image>::SharedPtr image_pub_;
    rclcpp::Publisher<sensor_msgs::msg::CompressedImage>::SharedPtr compressed_pub_;

    // OpenCV VideoCapture with GStreamer backend
    cv::VideoCapture cap_;
    mutable std::mutex cap_mutex_;  // Protects cap_ access across threads

    // Frame processing thread
    std::thread frame_thread_;
    std::atomic<bool> frame_thread_running_{false};

    // Camera parameters
    std::string device_path_;
    int width_;
    int height_;
    double fps_;

    // Compressed image publishing settings
    bool publish_compressed_{false};
    int compressed_frame_interval_{5};
    int compressed_frame_counter_{0};
};

}  // namespace mars_cam
