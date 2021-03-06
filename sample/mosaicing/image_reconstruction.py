# DVS image reconstruction demo
#
# Given DVS events and camera poses (rotational motion), reconstruct the
# gradient map that caused the events.
#
# Céline Nauer, Institute of Neuroinformatics, University of Zurich
# Joël Bachmann, ETH
# Nik Dennler, Institute of Neuroinformatics, University of Zurich
#
# Original MATLAB code by
#  Guillermo Gallego
#  Robotics and Perception Group
#  University of Zurich

import os
import time
import math

from scipy import io
import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("TkAgg")
import matplotlib.pyplot as plt

# Provide function handles to convert from rotation matrix to axis-angle and vice-versa
import sample.helpers.integration_methods as integration_methods
import sample.helpers.coordinate_transforms as coordinate_transforms


## Run settings:
num_events_batch = 3000
num_events_display = 600000
num_batches_display = math.floor(num_events_display / num_events_batch)
scale_res = 1  # Use Zweierpotenz
plot_events_animation = False
plot_events_pm_animation = False

# Methods used:
# 1) Select measurement function used for brightness gradient estimation
#    via Extended Kalman Filter (EKF). Options are:
#    'contrast'    : Constrast criterion (Gallego et al. arXiv 2015)
#    'event_rate'  : Event rate criterion (Kim et al. BMVC 2014)
measurement_criterion = "contrast"


## ___Dataset___

time_0 = time.time()
# Data directories
data_dir = "../data/synth1"
calibration_dir = "../data/calibration"
output_dir = "../../output"
images_dir = os.path.join(
    output_dir, "{0}pbatch_{1}".format(num_events_batch, measurement_criterion)
)
print(images_dir)
if not os.path.exists(images_dir):
    os.makedirs(images_dir)


# Calibration data
dvs_calibration = io.loadmat(
    os.path.join(calibration_dir, "DVS_synth_undistorted_pixels.mat")
)
# Dictionary with entries ['__header__', '__version__', '__globals__',
#                          'Kint', 'dist_coeffs', 'image_height', 'image_width',
#                          'pixels_grid', 'undist_pix', 'undist_pix_calibrated',
#                          'undist_pix_mat_x', 'undist_pix_mat_y'])

dvs_parameters = {"sensor_height": 128, "sensor_width": 128, "contrast_threshold": 0.45}


## ___Main algorithmic parameters___

# Size of the output reconstructed image mosaic (panorama)
output_height = int(1024 / scale_res)
output_width = 2 * output_height


# Set the expected noise level (variance of the measurements).
if measurement_criterion == "contrast":
    var_R = 0.17 ** 2  # units[C_th] ^ 2
if measurement_criterion == "event_rate":
    var_R = 1e2 ** 2  # units[1 / second] ^ 2

# 2) Select the gradient integration method. Options are:
integration_method = "frankotchellappa"


## Loading Events
print("Loading Events")
filename_events = os.path.join(data_dir, "events.txt")
# Events have time in whole sec, time in ns, x in ]0, 127[, y in ]0, 127[
events = pd.read_csv(
    filename_events, delimiter=" ", header=None, names=["sec", "nsec", "x", "y", "pol"]
)
print(events.head())

num_events = events.size
# print("Number of events in file: ", num_events)

# Remove time of offset
first_event_sec = events.loc[0, "sec"]
first_event_nsec = events.loc[0, "nsec"]
events["t"] = (
    events["sec"] - first_event_sec + 1e-9 * (events["nsec"] - first_event_nsec)
)
events = events[["t", "x", "y", "pol"]]
# print("Head: \n", events.head(10))
# print("Tail: \n", events.tail(10))

##Loading Camera poses
print("Loading Camera Orientations")
filename_events = os.path.join(data_dir, "poses.txt")
poses = pd.read_csv(
    filename_events,
    delimiter=" ",
    header=None,
    names=["sec", "nsec", "x", "y", "z", "qx", "qy", "qz", "qw"],
)
print(poses.head())
num_poses = poses.size
print("Number of poses in file: ", num_poses)

poses["t"] = (
    poses["sec"] - first_event_sec + 1e-9 * (poses["nsec"] - first_event_nsec)
)  # time_ctrl in MATLAB
poses = poses[["t", "qw", "qx", "qy", "qz"]]  # Quaternions
print("Head: \n", poses.head(10))
print("Tail: \n", poses.tail(10))

# Convert quaternions to rotation matrices and save in a dictionary
rotmats_dict = coordinate_transforms.q2R_dict(poses)

## Image reconstruction using pixel-wise EKF
# Input: events, contrast threshold and camera orientation (discrete poses + interpolation)
# Output: reconstructed intensity image
# profile('on','-detail','builtin','-timer','performance')
starttime = time.time()


# Variables related to the reconstructed image mosaic. EKF initialization

# Gradient map
grad_map = {}
grad_map["x"] = np.zeros((output_height, output_width))
grad_map["y"] = np.zeros((output_height, output_width))

# Covariance matrix of each gradient pixel
grad_initial_variance = 10
grad_map_covar = {}
grad_map_covar["xx"] = np.ones((output_height, output_width)) * grad_initial_variance
grad_map_covar["xy"] = np.zeros((output_height, output_width))
grad_map_covar["yx"] = np.zeros((output_height, output_width))
grad_map_covar["yy"] = np.ones((output_height, output_width)) * grad_initial_variance

# For efficiency, a structure, called event map, contains for every pixel
# the time of the last event and its rotation at that time.
s = {"sae": -1e-6, "rotation": np.zeros((3, 3))}
s["rotation"].fill(np.NaN)
event_map = np.zeros((dvs_parameters["sensor_height"], dvs_parameters["sensor_width"]))
event_map = event_map.tolist()
for i in range(dvs_parameters["sensor_height"]):
    for j in range(dvs_parameters["sensor_width"]):
        event_map[i][j] = s.copy()
event_map = np.array(event_map)
# print(event_map.shape)

rotmats_1stkey = list(rotmats_dict.keys())[0]
rot0 = rotmats_dict[rotmats_1stkey]  # to later center the map around the first pose
one_vec = np.ones((num_events_batch, 1))
Id = np.eye(2)

## Processing events
print("Processing events")
first_plot = True  # for efficient plotting

iEv = 0  # event counter
iBatch = 1  # packet-of-events counter

i = 0
while True:
    if iEv + num_events_batch > num_events:
        print("No more events")
        break

    #% Get batch of events
    events_batch = events[iEv : iEv + num_events_batch]
    iEv = iEv + num_events_batch

    events_batch_pos = events_batch[events_batch["pol"] == 1]
    events_batch_neg = events_batch[events_batch["pol"] == 0]

    t_events_batch = events_batch["t"]
    x_events_batch = events_batch["x"]
    y_events_batch = events_batch["y"]
    pol_events_batch = 2 * (events_batch["pol"] - 0.5)

    ## Get the two map points correspondig to each event and update the event map (time and rotation of last event)

    # Get time of previous event at same DVS pixel
    idx_to_mat = x_events_batch * dvs_parameters["sensor_height"] + y_events_batch

    t_prev_batch = np.array(
        [event_map[y, x]["sae"] for x, y in zip(x_events_batch, y_events_batch)]
    ).T

    # Get (interpolated) rotation of current event
    first_idx = t_events_batch.index[0]
    last_idx = t_events_batch.index[-1]
    t_ev_mean = (t_events_batch.loc[first_idx] + t_events_batch.loc[last_idx]) * 0.5
    if t_ev_mean > poses["t"].iloc[-1]:
        print("Event later than last known pose")
        break  # event later than last known pose

    Rot = coordinate_transforms.rotation_interpolation(
        poses["t"], rotmats_dict, t_ev_mean
    )

    try:
        bearing_vec = np.vstack(
            (
                dvs_calibration["undist_pix_calibrated"][idx_to_mat, :].T[0],
                dvs_calibration["undist_pix_calibrated"][idx_to_mat, :].T[1],
                one_vec[:, 0],
            )
        )  # 3xN
    except ValueError:
        print(dvs_calibration["undist_pix_calibrated"][idx_to_mat, :].T[0].shape)
        print(dvs_calibration["undist_pix_calibrated"][idx_to_mat, :].T[1].shape)
        print(one_vec[:, 0].shape)
        print("Event  # {}".format(iEv))
        break

    # Get map point corresponding to current event
    rotated_vec = rot0.T.dot(Rot).dot(bearing_vec)
    pm = coordinate_transforms.project_equirectangular_projection(
        rotated_vec, output_width, output_height
    )

    #  Get map point corresponding to previous event at same pixel
    rotated_vec_prev = np.zeros(rotated_vec.shape)
    for ii in range(num_events_batch):
        Rot_prev = event_map[y_events_batch.iloc[ii]][x_events_batch.iloc[ii]][
            "rotation"
        ].copy()
        rotated_vec_prev[:, ii] = rot0.T.dot(Rot_prev).dot(bearing_vec[:, ii])

        # Update last rotation and time of event(SAE)
        event_map[y_events_batch.iloc[ii]][x_events_batch.iloc[ii]][
            "sae"
        ] = t_events_batch.iloc[ii]
        event_map[y_events_batch.iloc[ii]][x_events_batch.iloc[ii]]["rotation"] = Rot

    pm_prev = coordinate_transforms.project_equirectangular_projection(
        rotated_vec_prev, output_width, output_height
    )

    if (t_prev_batch[-1] < 0) or (t_prev_batch[-1] < poses["t"][0]):
        # initialization phase. Fill in event_map
        continue

    #  Discard nan values
    mask_uninitialized = np.isnan(pm_prev[0, :]) | np.isnan(pm_prev[1, :])
    num_uninitialized = sum(mask_uninitialized)

    if True:
        # (num_uninitialized > 0):
        # % Delete uninitialized events
        t_events_batch = np.array(t_events_batch.iloc[~mask_uninitialized].tolist())
        t_prev_batch = np.array(t_prev_batch[~mask_uninitialized].tolist())

        pol_events_batch = np.array(pol_events_batch[~mask_uninitialized].tolist())
        pm_ = pm.copy()
        pm_prev_ = pm_prev.copy()
        pm = []
        pm_prev = []
        for row in pm_:
            pm.append(row[~mask_uninitialized].tolist())
        for row in pm_prev_:
            pm_prev.append(row[~mask_uninitialized].tolist())
        pm = np.array(pm)
        pm_prev = np.array(pm_prev)

    # Get time since previous event at same pixel
    tc = t_events_batch - t_prev_batch
    event_rate = 1.0 / (tc + 1e-12)  #% measurement or observation(z)

    # Get velocity vector
    vel = (pm - pm_prev) * event_rate

    ## Extended Kalman Filter (EKF) for the intensity gradient map.
    # Get gradient and covariance at current map points pm
    ir = np.floor(pm[1, :]).astype(int)  # row is y coordinate
    ic = np.floor(pm[0, :]).astype(int)  # col is x coordinate

    gm = np.array([[grad_map["x"][i][j], grad_map["y"][i][j]] for i, j in zip(ir, ic)])
    Pg = np.array(
        [
            [
                grad_map_covar["xx"][i][j],
                grad_map_covar["xy"][i][j],
                grad_map_covar["yx"][i][j],
                grad_map_covar["yy"][i][j],
            ]
            for i, j in zip(ir, ic)
        ]
    )

    # EKF update
    if measurement_criterion == "contrast":
        # Use contrast as measurement function
        dhdg = (
            vel.T * np.array([tc * pol_events_batch, tc * pol_events_batch]).T
        )  # derivative of measurement function

        nu_innovation = dvs_parameters["contrast_threshold"] - np.sum(dhdg * gm, axis=1)
    else:
        # Use the event rate as measurement function
        dhdg = (
            vel.T
            / np.array(
                [
                    dvs_parameters["contrast_threshold"] * pol_events_batch,
                    dvs_parameters["contrast_threshold"] * pol_events_batch,
                ]
            ).T
        )  # deriv. of measurement function
        nu_innovation = event_rate - np.sum(dhdg * gm, axis=1)

    Pg_dhdg = np.array(
        [
            Pg[:, 0] * dhdg[:, 0] + Pg[:, 1] * dhdg[:, 1],
            Pg[:, 2] * dhdg[:, 0] + Pg[:, 3] * dhdg[:, 1],
        ]
    ).T

    S_covar_innovation = dhdg[:, 0] * Pg_dhdg[:, 0] + dhdg[:, 1] * Pg_dhdg[:, 1] + var_R
    Kalman_gain = Pg_dhdg / np.array([S_covar_innovation, S_covar_innovation]).T

    # Update gradient and covariance
    gm = gm + Kalman_gain * np.array([nu_innovation, nu_innovation]).T
    Pg = (
        Pg
        - np.array(
            [
                Pg_dhdg[:, 0] * Kalman_gain[:, 0],
                Pg_dhdg[:, 0] * Kalman_gain[:, 1],
                Pg_dhdg[:, 1] * Kalman_gain[:, 0],
                Pg_dhdg[:, 1] * Kalman_gain[:, 1],
            ]
        ).T
    )

    k = 0
    for i, j in zip(ir, ic):
        grad_map["x"][i][j] = gm[k, 0]
        grad_map["y"][i][j] = gm[k, 1]
        grad_map_covar["xx"][i][j] = Pg[k, 0]
        grad_map_covar["xy"][i][j] = Pg[k, 1]
        grad_map_covar["yx"][i][j] = Pg[k, 2]
        grad_map_covar["yy"][i][j] = Pg[k, 3]
        k += 1

    iBatch = iBatch + 1

    if plot_events_animation or plot_events_pm_animation:
        print("Update display: Event # {}".format(iEv))
        idx_pos = pol_events_batch > 0
        idx_neg = pol_events_batch < 0

        trace_map = grad_map_covar["xx"] + grad_map_covar["yy"]
        grad_map_clip = {}
        grad_map_clip["x"] = grad_map["x"]
        grad_map_clip["y"] = grad_map["y"]
        mask = trace_map > 0.05  # reconstruct only gradients with small covariance
        grad_map_clip["x"][mask] = 0
        grad_map_clip["x"][mask] = 0

        rec_image = integration_methods.frankotchellappa(
            grad_map_clip["x"], grad_map_clip["y"]
        )
        rec_image = rec_image - np.mean(rec_image)

        if first_plot:
            first_plot = False

            if plot_events_animation:
                # Iteratively plot events on sensor image
                fig_events_raw = plt.figure(0)
                ax_events_raw = fig_events_raw.add_subplot(111)
                events_p, = ax_events_raw.plot(
                    events_batch_pos["x"], events_batch_pos["y"], ",b"
                )
                events_n, = ax_events_raw.plot(
                    events_batch_neg["x"], events_batch_neg["y"], ",r"
                )
                plt.xlim([0, dvs_parameters["sensor_width"]])
                plt.ylim([0, dvs_parameters["sensor_height"]])
                plt.title("Events on Sensor")

                plt.ion()
                plt.show()

            if plot_events_pm_animation:
                # Iteratively plot points on panoramic image, colored according to polarity
                fig_events = plt.figure(1)
                ax_events = fig_events.add_subplot(111)
                h_map_pts_p, = ax_events.plot(pm[0, idx_pos], pm[1, idx_pos], ",b")
                h_map_pts_n, = ax_events.plot(pm[0, idx_neg], pm[1, idx_neg], ",r")
                plt.xlim([0, output_width])
                plt.ylim([0, output_height])
                plt.title("Map points from events")

                plt.ion()
                plt.show()

            # Display reconstructed image
            # fig_reconstructed = plt.figure(2)
            # ax_reconstructed = fig_reconstructed.add_subplot(111)
            # maximum = np.max(np.abs(rec_image))
            # minimum = np.min(np.abs(rec_image))
            # h_img = plt.imshow(rec_image / maximum, cmap=plt.cm.binary, vmin=-1, vmax=1)

            # Display one of the gradient images
            # fig_gradient = plt.figure(3)
            # ax_gradient = fig_gradient.add_subplot(111)
            # h_gx = plt.imshow(grad_map['x'] / np.std(grad_map['x']), cmap=plt.cm.binary, vmin=-5, vmax=5)
            # # plt.xlim([0, output_width])
            # # plt.ylim([0, output_height])
            # plt.xlim([0, output_width])
            # plt.ylim([0, output_height])
            # plt.ion()
            # plt.show()

            # print("MAX: ", np.max([np.abs(np.max(rec_image[0])), np.abs(np.min(rec_image[0]))]))
            # print(np.max(np.abs(rec_image[0])))

        else:
            if plot_events_animation:
                events_p.set_data(events_batch_pos["x"], events_batch_pos["y"])
                events_n.set_data(events_batch_neg["x"], events_batch_neg["y"])
                plt.savefig(
                    output_dir
                    + "/animation/events_raw/"
                    + "fig_events_{}.png".format(str(iEv).zfill(10))
                )

            if plot_events_pm_animation:
                h_map_pts_p.set_data(pm[0, idx_pos], pm[1, idx_pos])
                h_map_pts_n.set_data(pm[0, idx_neg], pm[1, idx_neg])
                plt.savefig(
                    output_dir
                    + "/animation/events_pm/"
                    + "fig_events_{}.png".format(str(iEv).zfill(10))
                )
            # maximum = np.max(np.abs(rec_image))
            # minimum = np.min(np.abs(rec_image))
            # h_img.set_data(rec_image / maximum)

            # h_gx.set_data(grad_map['x'] / np.std(grad_map['x']))
            # ax_events.relim()
            plt.pause(0.01)

endtime = time.time()
print("Done")
print("Elapsed time: {} seconds".format(endtime - time_0))


# Display in separate figure
print("Total summed Events # {}".format(iEv))
idx_pos = pol_events_batch > 0
idx_neg = pol_events_batch < 0

trace_map = grad_map_covar["xx"] + grad_map_covar["yy"]
grad_map_clip = {}
grad_map_clip["x"] = grad_map["x"]
grad_map_clip["y"] = grad_map["y"]
mask = trace_map > 0.05  # % reconstruct only gradients with small covariance
grad_map_clip["x"][mask] = 0
grad_map_clip["x"][mask] = 0
rec_image = integration_methods.frankotchellappa(grad_map_clip["x"], grad_map_clip["y"])
rec_image = -rec_image + np.mean(rec_image)

rec_image_normalized = rec_image / np.max(np.abs(rec_image))
fig_normalized = plt.figure(1)
plt.imshow(rec_image_normalized, cmap=plt.cm.binary)
plt.title("Reconstructed image (log)")
plt.savefig(os.path.join(images_dir, "reconstructed_log.png"))
plt.show()
#
rec_image_exp = np.exp(0.001 + rec_image)
fig_normalized_linear = plt.figure(2)
plt.imshow(rec_image_exp, cmap=plt.cm.binary)
plt.title("Reconstructed image (linear)")
plt.savefig(os.path.join(images_dir, "reconstructed_linear.png"))
plt.show()

fig_gradientx = plt.figure(3)
h_gx = plt.imshow(
    grad_map["x"] / np.std(grad_map["x"]), cmap=plt.cm.binary, vmin=-5, vmax=5
)
plt.title("Gradient in X")
plt.savefig(os.path.join(images_dir, "gradient_x.png"))
plt.show()

fig_gradienty = plt.figure(4)
h_gx = plt.imshow(
    grad_map["y"] / np.std(grad_map["y"]), cmap=plt.cm.binary, vmin=-5, vmax=5
)
plt.title("Gradient in Y")
plt.savefig(os.path.join(images_dir, "gradient_y.png"))
plt.show()

fig_tracemap = plt.figure(5)
h_gx = plt.imshow(trace_map / np.max(trace_map), cmap=plt.cm.binary, vmin=0, vmax=1)
plt.title("Trace of Covariance")
plt.savefig(os.path.join(images_dir, "trace.png"))
plt.show()

# g_ang = -1*np.arctan2(grad_map['y'], grad_map['x'])
# g_grad = np.sqrt(np.power(grad_map['x'], 2) + np.power(grad_map['y'], 2))
# g_grad_unit = g_grad/1.
# g_grad_unit[g_grad_unit > 1.0] = 1.0
# g_ang_unit = g_ang/360. + 0.5

np.save("intensity_map.npy", rec_image)
