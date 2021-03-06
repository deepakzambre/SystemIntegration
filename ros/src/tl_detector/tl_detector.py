#!/usr/bin/env python
import rospy
from std_msgs.msg import Int32
from geometry_msgs.msg import PoseStamped, Pose
from styx_msgs.msg import TrafficLightArray, TrafficLight
from styx_msgs.msg import Lane
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
from light_classification.tl_classifier import TLClassifier
import tf
import cv2
import yaml
import math
import uuid
import numpy as np
from scipy.spatial import KDTree
import rospkg
import os

STATE_COUNT_THRESHOLD = 3
LIGHT_DISTANCE_THRESHOLD = 50

GENERATE_DATASET = True
IMAGE_CAPTURE_DISTANCE = 100

class TLDetector(object):
    def __init__(self):
        rospy.init_node('tl_detector', log_level=rospy.DEBUG)

        self.pose = None
        self.previous_pose = None
        self.waypoints = None
        self.waypoints_2d = None
        self.waypoint_tree = None
        self.camera_image = None
        self.lights = []

        self.dataset_path = rospkg.get_ros_package_path().split(':')[0] + '/images/'
        if GENERATE_DATASET:
            if not os.path.exists(self.dataset_path):
                os.makedirs(self.dataset_path)
            rospy.loginfo("Dataset will be created at %s", self.dataset_path)
            self.dataset_file = open(self.dataset_path + 'img_dataset.tsv', 'a')
            self.image_write_ts = rospy.Time()

        sub1 = rospy.Subscriber('/current_pose', PoseStamped, self.pose_cb)
        sub2 = rospy.Subscriber('/base_waypoints', Lane, self.waypoints_cb)

        '''
        /vehicle/traffic_lights provides you with the location of the traffic light in 3D map space and
        helps you acquire an accurate ground truth data source for the traffic light
        classifier by sending the current color state of all traffic lights in the
        simulator. When testing on the vehicle, the color state will not be available. You'll need to
        rely on the position of the light and the camera image to predict it.
        '''
        sub3 = rospy.Subscriber('/vehicle/traffic_lights', TrafficLightArray, self.traffic_cb)
        sub6 = rospy.Subscriber('/image_color', Image, self.image_cb)

        config_string = rospy.get_param("/traffic_light_config")
        self.config = yaml.load(config_string)

        self.upcoming_red_light_pub = rospy.Publisher('/traffic_waypoint', Int32, queue_size=1)

        self.bridge = CvBridge()
        self.light_classifier = TLClassifier()
        self.listener = tf.TransformListener()

        self.state = TrafficLight.UNKNOWN
        self.last_state = TrafficLight.UNKNOWN
        self.last_wp = -1
        self.state_count = 0

        rospy.spin()

    def pose_cb(self, msg):
        self.previous_pose = self.pose
        self.pose = msg

    def waypoints_cb(self, waypoints):
        self.waypoints = waypoints
        if not self.waypoints_2d:
            self.waypoints_2d = [[waypoint.pose.pose.position.x, waypoint.pose.pose.position.y] for waypoint in waypoints.waypoints]
            self.waypoint_tree = KDTree(self.waypoints_2d)

    def traffic_cb(self, msg):
        self.lights = msg.lights

    def image_cb(self, msg):
        """Identifies red lights in the incoming camera image and publishes the index
            of the waypoint closest to the red light's stop line to /traffic_waypoint

        Args:
            msg (Image): image from car-mounted camera

        """
        self.has_image = True
        self.camera_image = msg
        light_wp, state = self.process_traffic_lights()

        '''
        Publish upcoming red lights at camera frequency.
        Each predicted state has to occur `STATE_COUNT_THRESHOLD` number
        of times till we start using it. Otherwise the previous stable state is
        used.
        '''
        if self.state != state:
            self.state_count = 0
            self.state = state
        elif self.state_count >= STATE_COUNT_THRESHOLD:
            self.last_state = self.state
            light_wp = light_wp if state == TrafficLight.RED else -1
            self.last_wp = light_wp
            self.upcoming_red_light_pub.publish(Int32(light_wp))
        else:
            self.upcoming_red_light_pub.publish(Int32(self.last_wp))
        self.state_count += 1

    def get_closest_waypoint(self, x, y):
        """Identifies the closest path waypoint to the given position
            https://en.wikipedia.org/wiki/Closest_pair_of_points_problem
        Args:
            pose (Pose): position to match a waypoint to

        Returns:
            int: index of the closest waypoint in self.waypoints

        """

        return self.waypoint_tree.query([x, y], 1)[1]

    def get_light_state(self, light):
        """Determines the current color of the traffic light

        Args:
            light (TrafficLight): light to classify

        Returns:
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        if(not self.has_image):
            self.prev_light_loc = None
            return False

        cv_image = self.bridge.imgmsg_to_cv2(self.camera_image, "bgr8")

        #Get classification
        return self.light_classifier.get_classification(cv_image)

    def try_image_capture(self, light, light_distance):

        if light_distance > IMAGE_CAPTURE_DISTANCE:
            return

        cv_image = self.bridge.imgmsg_to_cv2(self.camera_image, "bgr8")
        curr_time = rospy.get_rostime()
        if self.has_image and (self.state != light.state or rospy.Time.now() >= (self.image_write_ts + rospy.Duration(0.2))):
            self.image_write_ts = rospy.Time.now()
            filname = str(uuid.uuid4()) + '.jpg'
            filepath = self.dataset_path + filname
            cv2.imwrite(filepath, cv_image)
            self.dataset_file.write(filname + "\t" + str(light.state) + "\n")

        return

    def process_traffic_lights(self):
        """Finds closest visible traffic light, if one exists, and determines its
            location and color

        Returns:
            int: index of waypoint closes to the upcoming stop line for a traffic light (-1 if none exists)
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        light_distance = float('inf')
        stop_line_idx = -1
        closest_light = None

        # List of positions that correspond to the line to stop in front of for a given intersection
        stop_line_positions = self.config['stop_line_positions']
        if not None in (self.pose, self.previous_pose):

            for idx, light in enumerate(self.lights):
                stop_line_position = stop_line_positions[idx]

                diff_x = self.pose.pose.position.x - stop_line_position[0]
                diff_y = self.pose.pose.position.y - stop_line_position[1]
                distance = math.sqrt(diff_x * diff_x + diff_y * diff_y)

                previous_car_vect = np.array([self.previous_pose.pose.position.x, self.previous_pose.pose.position.y])
                car_vect = np.array([self.pose.pose.position.x, self.pose.pose.position.y])
                light_vect = np.array([stop_line_position[0], stop_line_position[1]])

                val = np.dot(light_vect - car_vect, car_vect - previous_car_vect)
                if val > 0 and distance < light_distance:
                    light_distance = distance
                    closest_light = light
                    stop_line_idx = idx


        if closest_light:

            if GENERATE_DATASET:
                self.try_image_capture(closest_light, light_distance)

            if light_distance < LIGHT_DISTANCE_THRESHOLD:
                rospy.logdebug("next %s light @ %s m distance", str(closest_light.state), light_distance)
                light_state = self.get_light_state(closest_light)
                stop_line_position = stop_line_positions[stop_line_idx]
                closest_light_wp_idx = self.get_closest_waypoint(stop_line_position[0], stop_line_position[1])
                return closest_light_wp_idx, light_state

        return -1, TrafficLight.UNKNOWN

if __name__ == '__main__':
    try:
        TLDetector()
    except rospy.ROSInterruptException:
        rospy.logerr('Could not start traffic node.')
