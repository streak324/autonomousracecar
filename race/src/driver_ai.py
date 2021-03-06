#!/usr/bin/env python2
# racecar AI for Driver

import rospy
import math
from race.msg import drive_param
from sensor_msgs.msg import LaserScan
from std_msgs.msg import Bool
from std_msgs.msg import String
from std_msgs.msg import Float32

import constants

import time
# useful functions
def average(array):
    total = 0
    for value in array:
        total += value
    return total / len(array)

def dist_between(pos1, pos2):
    return math.sqrt( (pos1[0]-pos2[0])**2 + (pos1[1]-pos2[1])**2 )

class Car:
    def __init__(self):
        self.lidar = []
        self._angle = 0 # used by turningProgram
        self._position = 0 # used by turningProgram
        self.motorSpeed = 0
        self.turnAngle = 0
        self.velocity = 0.0
        self.fricCoeff = 10 # coefficient of friction between ground and tires
        self.slowdown_distance = 5
        self.carLength = 0.5 # length of car determines smallest turn radius
        self.reading_number = 1 # ray casts per degree (2 = 360 readings)
        self.speed_factor = 0.4 # car's speed limit: range 0.2 (min) to 1
        self.lidar = [10.0]*(180*self.reading_number+1) #want lidar range to be 0 to 180 inclusive
        self.init_time = rospy.get_time()
        self.motorKill = False;
        self.stopTime = 0; # timestamp for breaking
        self.alert = "none"

    def get_dur(self):
        return str("%.3f" % (rospy.get_time() - self.init_time))
    
    def changeMotorSpeed(self, val):
        # print("requested motorSpeed: " + str(100*val/2.0))
        self.velocity = val
        if(self.velocity > self.speed_factor): # cropping motor speed here just for now
            self.velocity = self.speed_factor

        if(self.motorKill):
            # print("[" + self.get_dur() + "] Alert: motors killed")
            self.alert = "Motors killed"
            if(self.stopTime > time.time() and self.motorSpeed != 0):
                self.velocity = -1
                # print("-50 motor speed")
            else:
                # print("0 motor speed")
                self.velocity = 0

        self.motorSpeed = 100*self.velocity/2.0

    def changeTurnAngle(self, ang):
        # clip turn angle to +/- pi/4
        if (ang > math.pi/4):
            self.turnAngle = math.pi/4
            return
        if (ang < -math.pi/4):
            self.turnAngle = -math.pi/4
            return
        self.turnAngle = ang

    def __del__(self):
        self.changeMotorSpeed(0)
        self.changeTurnAngle(0)

class RacecarAI:
    '''This is the Driver module that controls the car's basic functions
    It is designed to operate with or without a codriver.
    Without a codriver, the driver will simply drive slowly and safely, avoiding
    obstances and performing turns when necessary. It is designed to be lightweight
    and free of hardcoding.
    '''

    def __init__(self, car, codriver, dumb):
        # codriver - bool that enables codriver
        # dumb - bool that indicates that driver should only avoid collisions
        self.dumb = dumb
        self.state = "auto"
        self.car = car
        self.codriver = None
        self.fricCoeff = 10 # this will have to be adjusted to a realistic value
        self.safetyMode = False

        ''' Variables for collision avoidance'''
        self.collisionAvoid = False
        self.obsDist = 0
        self.obsAngle = 0
        
        self.start_angle = self.car._angle # used to keep track of how much the car has turned at nodes, updated in autoProgram
        self.start_pos = self.car._position # used to record distance traveled by car during collision avoidance mode
        
        self.pub = rospy.Publisher('drive_parameters', drive_param, queue_size=10) # ros publisher
        self.pub2 = rospy.Publisher('alert', String, queue_size=10)
        self.pub3 = rospy.Publisher('state', String, queue_size=10)
        self.pub4 = rospy.Publisher('motor', Float32, queue_size=10)
        self.pub5 = rospy.Publisher('angle', Float32, queue_size=10)
        

        self.listener()

    def kill_motors(self, data):
        # print("Alert: motors killed")
        self.car.motorKill = True
        self.car.stopTime = time.time() + 0.5
    #-------------------------------------------------------------------------------------

    def moveTowardsLongestDist(self, scope, right_bound, left_bound):
        ''' Uses the lidar to find the angle with the longest distance reading, and returns that angle. The angle is
        relative to the direction the car is facing. 0 degrees should be straight forward. Left angles are negative and
        right angles are positive.`
        scope - limits the band of view which that car is using to make its decisions
        right_bound, left_bound - bottom and top boundary on the lidar indices that are taken into consideration 
        '''
        angle = math.pi / len(self.car.lidar)
        maximum = 0
        max_index = 0
        for reading in range(right_bound+scope, left_bound-scope):
            if(self.car.lidar[reading] >= maximum):
                maximum = self.car.lidar[reading]
                max_index = reading
            
        new_angle = angle*max_index - math.pi/2 # subtract PI/2 to make angle=0 -> angle=-PI/2
        return new_angle

    #-------------------------------------------------------------------------------------

    def _moveAwayFromWall(self, dist, angle):
        #if car is too close to a wall, modify angle to pull away
        left = average(self.car.lidar[0:5*self.car.reading_number])
        right = average(self.car.lidar[-5*self.car.reading_number:len(self.car.lidar)])
        # dist will be scaled by number of carLengths (currently 1x)
        if(left < dist):
            # print("[" + self.car.get_dur() + "] Alert: left " + str(left))
            self.alert = "left " + str(left)
            return angle + math.pi/12
        elif(right < dist):
            # print("[" + self.car.get_dur() + "] Alert: right "+ str(right))
            self.alert = "right " + str(right)
            return angle - math.pi/12
        else:
            self.alert = "none"
            return angle

    def _getFrontDist(self, angle=0):
        middle = len(self.car.lidar)//2
        # relative angle ranges from -pi/2 to pi/2
        lidar_beam_angle = (math.pi / len(self.car.lidar))
        #determine the range of values to scan
        scope = int(math.atan(1) / lidar_beam_angle) + 1
        # this converts relative angle to corresponding LIDAR index
        index = int(angle / lidar_beam_angle + middle)
        
        front_dist = self.car.lidar[index]
        for i in range(-scope,scope+1):
            if(self.car.lidar[index+i]*math.sin(abs(i)*lidar_beam_angle) < self.car.carLength/2):
                if(self.car.lidar[index+i] < front_dist):
                    front_dist = self.car.lidar[index+i]
        return front_dist


    def _getFrontAngle(self, angle=0):
        middle = len(self.car.lidar)//2
        # relative angle ranges from -pi/2 to pi/2
        lidar_beam_angle = (math.pi / len(self.car.lidar))
        #determine the range of values to scan
        scope = int(math.atan(1) / lidar_beam_angle) + 1
        # this converts relative angle to corresponding LIDAR index
        index = int(angle / lidar_beam_angle + middle)
        slopes = []

        for i in range(-scope,scope+1):
            if(self.car.lidar[index+i]*math.sin(abs(i)*lidar_beam_angle) < self.car.carLength/2):
                # average of slopes between left and right lidar readings
                x1 = self.car.lidar[index+i] * math.cos(lidar_beam_angle)
                y1 = self.car.lidar[index+i] * math.sin(lidar_beam_angle)
                x2 = self.car.lidar[index+i+1] * math.cos(2*lidar_beam_angle)
                y2 = self.car.lidar[index+i+1] * math.sin(2*lidar_beam_angle)
                slopes.append( math.atan2((y2-y1) , (x2-x1)) )
        return average(slopes)


    def detectObstacle(self, front_dist):
        # Imminent obstacle that needs to be avoided
        # 4 car lengths away if travelling at 10 speed
        # 8 car lengths away if travelling at 20 speed
        lookaheadDistance = self.car.carLength * 5 * self.car.motorSpeed/10
        lookaheadDistance = max(lookaheadDistance, 2.5) # set lower bound to lookaheadDistance to 2
        print("lookahead: " + str(lookaheadDistance))
        if front_dist < lookaheadDistance:
            #print("obstacle detected " + str(front_dist)+ "m away")
            self.obsDist = front_dist
            return True
        else:
            return False
            
    def autoProgram(self):
        #CHECK FOR UNAVOIDABLE CRASH
        ''' If the distance reading directly in front of the car is less that the minimum turning radius
            given velocity of the car and friction, car cannot turn to avoid the crash
        '''
        front_dist = self._getFrontDist()

        self.collisionAvoid = self.detectObstacle(front_dist)
        if self.dumb:
            self.collisionAvoid = True
        
        # level 0 = normal operation
        # level 1 = initial slowdown
        # level 2 = slow approach
        # level 3 = avoid collision (collisionAvoid = true)
        front_angle = self._getFrontAngle()
        if front_dist < self.car.slowdown_distance * self.car.speed_factor:
            # level 2
            velocity = 0.2  # if obstacle up ahead but not imminent, lower speed to minimum
        elif front_dist < self.car.slowdown_distance * self.car.speed_factor * 1.4: #and front_angle < 2.0 and front_angle > 1.2:
            # level 1
            velocity = 0.0
        else:
            # level 0: SET CAR VELOCITY PROPORTIONAL TO FRONT DISTANCE
            # v = 0.2 (minimum) when front_dist = 5 car lengths (2.5m)
            # v = speed_factor (speed limit) when front_dist = 10m (lidar max)
            velocity = (front_dist - 2.5) / 7.5 * (self.car.speed_factor - 0.2) + 0.2 
        #print(velocity)
        self.car.changeMotorSpeed(velocity)

        #DETERMINE ANGLE TO TURN WHEELS TO
        angle = math.pi / (len(self.car.lidar)-1)
        left_slopes = []
        right_slopes = []
        # average of slopes between left and right lidar readings
        for i in range(5):
            left_x1 = self.car.lidar[i+1] * math.cos(angle)
            left_y1 = self.car.lidar[i+1] * math.sin(angle)
            left_x2 = self.car.lidar[i+2] * math.cos(2*angle)
            left_y2 = self.car.lidar[i+2] * math.sin(2*angle)
            left_slopes.append( math.atan2((left_y2-left_y1) , (left_x2-left_x1)) )
            right_x1 = self.car.lidar[-i-2] * math.cos(math.pi - angle)
            right_y1 = self.car.lidar[-i-2] * math.sin(math.pi - angle)
            right_x2 = self.car.lidar[-i-3] * math.cos(math.pi - 2*angle)
            right_y2 = self.car.lidar[-i-3] * math.sin(math.pi - 2*angle)
            right_slopes.append( math.atan2((right_y2-right_y1) , (right_x2-right_x1)) )

        # if in a straight hallway type path
        ''' If the angle of left and right wall are kinda the same'''
        if(abs(average(left_slopes) - average(right_slopes)) < math.pi/10):
            new_angle = (average(left_slopes) + average(right_slopes) - math.pi)/2
        else: # if not in a straight hallway type path...
            # intelligent auto driver behavior (the thing to improve)
            #new_angle = self.moveTowardsLongestDist(0, 0, len(self.car.lidar))
            new_angle = 0
            #print("not in straight hallway")

        #if car is too close to a wall, modify angle to pull away
        new_angle = self._moveAwayFromWall(self.car.carLength, new_angle)
                
        # turn the wheels of the car according to the new_angle    
        ''' The intensity of angle change is preportional to the difference in current angle and desired angle'''
        diff = abs(new_angle - self.car.turnAngle)
        if(new_angle > self.car.turnAngle): # right
            # todo, clarify 0.2 constant
            self.car.changeTurnAngle(self.car.turnAngle + 0.2*diff)
        elif(new_angle < self.car.turnAngle): # left
            self.car.changeTurnAngle(self.car.turnAngle - 0.2*diff)

        # update the start angle for decision nodes
        ''' During a turn, the start_angle is not updated, and is used a reference to measure how much the car has turned'''
        self.start_angle = self.car._angle
        self.start_pos = self.car._position

    def slowProgram(self):
        pass

    #-------------------------------------------------------------------------------------
    def clipAngle(self, angle, clipAmount):
        # angle is given in radians,will clip angle between -clipAmount and clipAmount
        ans = angle
        if angle > clipAmount:
            ans = clipAmount
        elif angle < -clipAmount:
            ans = -clipAmount
        return ans
    
    def _chooseAvoidAngle(self, dist, thresh, buffer):
        # make a list of tuples representing start and end indices of lidar segments
        segments = []
        start = 10
        end = 10
        _sum = 0
        cutoff = self.car.reading_number*25  # limit to 110 deg
        for i in range(cutoff,len(self.car.lidar)-cutoff):
            if(self.car.lidar[i] > dist+thresh):
                end = i+1
                _sum += self.car.lidar[i] #* (len(self.car.lidar)//2 - i)
            elif(start != end):
                segments.append( (start, end, _sum) )
                start = end = i+1
                _sum = 0
            else:
                start = end = i+1
        segments.append( (start, end, _sum) )
        # select segment of greatest area
        _max = 0
        index = 0
        for i in range(len(segments)):
            if(segments[i][2] > _max):
                _max = segments[i][2]
                index = i
        # get angle from the middle of the best segment
        size = segments[index][1]-segments[index][0]
        best_index = segments[index][0] + size//2
        angle = math.pi / len(self.car.lidar)
        ans = best_index * angle - math.pi/2
        #for seg in segments:
        #    print(seg)
        #print("chosen angle: ", ans)

        return ans

                
    def avoidCollision(self):
        # velocity of 0.2 is the lowest
        self.car.changeMotorSpeed(0.2)
        buffer = math.pi/15
        new_angle = self._chooseAvoidAngle(self.obsDist, 0.1, buffer)
        #print("avoid angle: "+str(new_angle))
        #new_angle = self._moveAwayFromWall(10, new_angle)

        diff = abs(new_angle - self.car.turnAngle)
        if(new_angle > self.car.turnAngle): # right
            # todo
            self.car.changeTurnAngle(self.car.turnAngle + diff*0.2)#weight*diff)
            #self.turningProgram()
        elif(new_angle < self.car.turnAngle): # left
            self.car.changeTurnAngle(self.car.turnAngle - diff*0.2)#weight*diff)
        # todo, clarify positions
        # dist = dist_between(self.start_pos, self.car._position)
        # print(dist)
        # print(self.start_pos)
        # if(dist > self.obsDist):
            # self.collisionAvoid = False
        self.collisionAvoid = self.detectObstacle(self._getFrontDist())

    #-------------------------------------------------------------------------------------

    def turningProgram(self):
        # CURRENTLY NOT USED
        #set car velocity preportional to front dist
        #front_dist = self.car.lidar[len(self.car.lidar)//2]
        motor_speed = 1
        self.car.changeMotorSpeed(motor_speed)

        self.detectObstacle(self._getFrontDist())
        
        #decide turning angle
        ''' This makes sure Driver does not turn too much at a node. Planned to make the turning cap 90 degress, but found
        that an underestimate like 30 degrees work a lot better. Turing program only has to nudge the drive toward
        the right direction, after that, the autoProgram can stabalize the car on its path much better.'''
        if(abs(self.start_angle - self.car._angle) <= math.pi/6): # if car hasnt turned 30 degrees yet
            if(self.state == "left"):
                #print("left")
                right = 0
                left = len(self.car.lidar)//2-1
                new_angle = self.moveTowardsLongestDist(0, right, left)
            elif(self.state == "right"):
                #print("right")
                right = len(self.car.lidar)//2+1
                left = len(self.car.lidar)
                new_angle = self.moveTowardsLongestDist(0, right, left)
        else:
            #print("TURNED ENOUGH")
            new_angle = self.moveTowardsLongestDist(15, 0, len(self.car.lidar))
            
        diff = abs(new_angle - self.car.turnAngle)
        if(new_angle > self.car.turnAngle): # right
            # todo, clarify 0.1 constant
            self.car.changeTurnAngle(self.car.turnAngle + 0.1)#*abs(max_index - self.car.reading_number//2))
        elif(new_angle < self.car.turnAngle): # left
            self.car.changeTurnAngle(self.car.turnAngle - 0.1)#*abs(max_index - self.car.reading_number//2))

    #-------------------------------------------------------------------------------------

    def safetyProgram(self):
        if(self.car.motorKill == False):
            self.car.motorKill = True
            self.car.stopTime = time.time() + 0.5
            # print("In motor kill")

    def checkEmergency(self):
        #if(self.car.velocity**2/(front_dist/3) > 9.81*self.fricCoeff):
        #    self.safetyMode = True
        front_dist = self._getFrontDist()
        # print("front_dist: ", front_dist)
        #print("velocity: ", self.car.velocity)
        #print("vel**2/front_dist: ", self.car.velocity**2/front_dist)
        if(self.car.velocity**2/front_dist > 0.07): # 0.03 is probably a good approx based readings without motors running
            # check slopes of obstacle in front of us
            front_angle = self._getFrontAngle()
            if(front_angle < 2.0 and front_angle > 1.2): # these values are the range of slopes perpendicular to the car
                self.safetyMode = True
        if(front_dist < 0.5): #always go to safety mode
            # print("DISTANCE LESS THAN 0.5")
            self.safetyMode = True
            #print("safety mode on")
        #print("front_angle: ", self._getFrontAngle())
    #-------------------------------------------------------------------------------------

    def main_funct(self):
        # check for a changed state from Codriver
        if self.codriver is not None:
            self.state = self.codriver.chooseState()
        
        # check if we are about to collide
        self.checkEmergency()
        if(self.safetyMode):
            self.safetyProgram()
        
        if(self.collisionAvoid):
            # print("[" + self.car.get_dur() + "] State: COLLISION AVOID")
            self.avoidCollision()
        elif(self.state == "auto"):
            # print("[" + self.car.get_dur() + "] State: auto")
            self.autoProgram()
        elif(self.state == "left" or self.state == "right"):
            #print(self.state)
            self.turningProgram()
        elif(self.state =="slow"):
            self.slowProgram()
        #print('motorspeed = ' + str(self.car.motorSpeed))
        self.publisher()

    #------------------------------------------------------------------------------------

    def update_lidar(self, data):
        #print(data)
        # LIDAR data: (index, distance), where index goes from 1080 to 0
        # from the right going CCW, with 0 as center, 270deg = 0, -270deg = 1080
        # updates car's lidar array with the new tuple, converting to 0 to 180*n
        for i in range(180,len(data.ranges)-180):
            # these are values behind the LIDAR, we will discard them
            mapped_index = int(-(180.0*self.car.reading_number/720)*(i-900))
            self.car.lidar[mapped_index] = data.ranges[i]
        self.main_funct()   # lidar data updates very quickly, maybe trigger main_funct some other way

    def publisher(self):
        #todo, map vel and angle to [-100, 100]
        msg = drive_param()
        msg.velocity = self.car.motorSpeed
        # print("[" + self.car.get_dur() + "] Motor: " + str(self.car.motorSpeed) + "%")
        msg.angle = 100*self.car.turnAngle/(math.pi/4) + 8
        #msg.angle = 0
        # print("[" + self.car.get_dur() + "] Angle: " + str(msg.angle))
        
        # msg.state = self.state
        # msg.alert = self.car.alert

        self.pub.publish(msg)
        self.pub2.publish(self.car.alert)
        if (self.collisionAvoid):
            self.pub3.publish("Collision Avoidance")
        else:
            self.pub3.publish(self.state)
        self.pub4.publish(self.car.motorSpeed)
        self.pub5.publish(msg.angle)

    def listener(self):
        rospy.Subscriber('scan', LaserScan, self.update_lidar) # calls driver main function when lidar is updated
        rospy.Subscriber('eBreak', Bool, self.kill_motors) # sets self.motorKill to true to stop motors for emergency

############################################################################

if __name__ == '__main__':
    rospy.init_node('driver', anonymous=True) # what is anonymous??
    car = Car()
    RacecarAI(car, False, False)
    rospy.spin()
