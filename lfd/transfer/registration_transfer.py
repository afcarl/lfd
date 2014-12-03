from __future__ import division

import copy
import settings
import json
import trajoptpy
import openravepy
import numpy as np
import sys
from lfd.demonstration import demonstration
from lfd.environment import sim_util
from lfd.registration import registration, tps
from lfd.transfer import transfer
from lfd.transfer import planning
from lfd.util import util

class RegistrationAndTrajectoryTransferer(object):
    def __init__(self, registration_factory, trajectory_transferer):
        self.registration_factory = registration_factory
        self.trajectory_transferer = trajectory_transferer

    def transfer(self, demo, test_scene_state, callback=None, plotting=False):
        """Registers demonstration scene onto the test scene and uses this registration to transfer the demonstration trajectory

        Args:
            demo: Demonstration that has the demonstration scene and the trajectory to transfer
            test_scene_state: SceneState of the test scene

        Returns:
            The transferred Trajectory
        """
        raise NotImplementedError

class TwoStepRegistrationAndTrajectoryTransferer(RegistrationAndTrajectoryTransferer):
    def transfer(self, demo, test_scene_state, callback=None, plotting=False):
        reg = self.registration_factory.register(demo, test_scene_state, callback=callback)
        test_aug_traj = self.trajectory_transferer.transfer(reg, demo, plotting=plotting)
        return test_aug_traj

class UnifiedRegistrationAndTrajectoryTransferer(RegistrationAndTrajectoryTransferer):
    def __init__(self, registration_factory, trajectory_transferer,
                 alpha=settings.ALPHA,
                 beta_pos=settings.BETA_POS,
                 gamma=settings.GAMMA,
                 use_collision_cost=settings.USE_COLLISION_COST,
                 init_trajectory_transferer=None):
        super(UnifiedRegistrationAndTrajectoryTransferer, self).__init__(registration_factory, trajectory_transferer)
        if not isinstance(registration_factory, registration.TpsRpmRegistrationFactory):
            raise NotImplementedError("UnifiedRegistrationAndTrajectoryTransferer only supports TpsRpmRegistrationFactory")
        if not isinstance(trajectory_transferer, transfer.FingerTrajectoryTransferer):
            raise NotImplementedError("UnifiedRegistrationAndTrajectoryTransferer only supports FingerTrajectoryTransferer")
        self.sim = trajectory_transferer.sim
        self.alpha = alpha
        self.beta_pos = beta_pos
        self.gamma = gamma
        self.use_collision_cost = use_collision_cost
        self.init_trajectory_transferer = init_trajectory_transferer

    def transfer(self, demo, test_scene_state, callback=None, plotting=False):
        reg = self.registration_factory.register(demo, test_scene_state, callback=callback)

        handles = []
        if plotting:
            demo_cloud = demo.scene_state.cloud
            test_cloud = reg.test_scene_state.cloud
            demo_color = demo.scene_state.color
            test_color = reg.test_scene_state.color
            handles.append(self.sim.env.plot3(demo_cloud[:,:3], 2, demo_color if demo_color is not None else (1,0,0)))
            handles.append(self.sim.env.plot3(test_cloud[:,:3], 2, test_color if test_color is not None else (0,0,1)))
            if self.sim.viewer:
              self.sim.viewer.Step()

        active_lr = ""
        for lr in 'lr':
            if lr in demo.aug_traj.lr2arm_traj and sim_util.arm_moved(demo.aug_traj.lr2arm_traj[lr]):
                active_lr += lr
        _, timesteps_rs = sim_util.unif_resample(np.c_[(1./settings.JOINT_LENGTH_PER_STEP) * np.concatenate([demo.aug_traj.lr2arm_traj[lr] for lr in active_lr], axis=1),
                                                       (1./settings.FINGER_CLOSE_RATE) * np.concatenate([demo.aug_traj.lr2finger_traj[lr] for lr in active_lr], axis=1)],
                                                 1.)
        demo_aug_traj_rs = demo.aug_traj.get_resampled_traj(timesteps_rs)

        if self.init_trajectory_transferer:
            warm_init_traj = self.init_trajectory_transferer.transfer(reg, demo, plotting=plotting)

        manip_name = ""
        flr2finger_link_names = []
        flr2demo_finger_pts_trajs_rs = []
        init_traj = np.zeros((len(timesteps_rs),0))
        for lr in active_lr:
            arm_name = {"l":"leftarm", "r":"rightarm"}[lr]
            finger_name = "%s_gripper_l_finger_joint"%lr

            if manip_name:
                manip_name += "+"
            manip_name += arm_name + "+" + finger_name

            if self.init_trajectory_transferer:
                init_traj = np.c_[init_traj, warm_init_traj.lr2arm_traj[lr], warm_init_traj.lr2finger_traj[lr]]
            else:
                init_traj = np.c_[init_traj, demo_aug_traj_rs.lr2arm_traj[lr], demo_aug_traj_rs.lr2finger_traj[lr]]

            if plotting:
                handles.append(self.sim.env.drawlinestrip(demo.aug_traj.lr2ee_traj[lr][:,:3,3], 2, (1,0,0)))
                handles.append(self.sim.env.drawlinestrip(demo_aug_traj_rs.lr2ee_traj[lr][:,:3,3], 2, (1,1,0)))
                transformed_ee_traj_rs = reg.f.transform_hmats(demo_aug_traj_rs.lr2ee_traj[lr])
                handles.append(self.sim.env.drawlinestrip(transformed_ee_traj_rs[:,:3,3], 2, (0,1,0)))
                if self.sim.viewer:
                  self.sim.viewer.Step()

            flr2demo_finger_pts_traj_rs = sim_util.get_finger_pts_traj(self.sim.robot, lr, (demo_aug_traj_rs.lr2ee_traj[lr], demo_aug_traj_rs.lr2finger_traj[lr]))
            flr2demo_finger_pts_trajs_rs.append(flr2demo_finger_pts_traj_rs)

            flr2transformed_finger_pts_traj_rs = {}
            flr2finger_link_name = {}
            flr2finger_rel_pts = {}
            for finger_lr in 'lr':
                flr2transformed_finger_pts_traj_rs[finger_lr] = reg.f.transform_points(np.concatenate(flr2demo_finger_pts_traj_rs[finger_lr], axis=0)).reshape((-1,4,3))
                flr2finger_link_name[finger_lr] = "%s_gripper_%s_finger_tip_link"%(lr,finger_lr)
                flr2finger_rel_pts[finger_lr] = sim_util.get_finger_rel_pts(finger_lr)
            flr2finger_link_names.append(flr2finger_link_name)

            if plotting:
                handles.extend(sim_util.draw_finger_pts_traj(self.sim, flr2demo_finger_pts_traj_rs, (1,1,0)))
                handles.extend(sim_util.draw_finger_pts_traj(self.sim, flr2transformed_finger_pts_traj_rs, (0,1,0)))
                if self.sim.viewer:
                  self.sim.viewer.Step()

        if not self.init_trajectory_transferer:
            # modify the shoulder joint angle of init_traj to be the limit (highest arm) because this usually gives a better local optima (but this might not be the right thing to do)
            dof_inds = sim_util.dof_inds_from_name(self.sim.robot, manip_name)
            joint_ind = self.sim.robot.GetJointIndex("%s_shoulder_lift_joint"%lr)
            init_traj[:,dof_inds.index(joint_ind)] = self.sim.robot.GetDOFLimits([joint_ind])[0][0]

        print "planning joint TPS and finger points trajectory following"
        test_traj, obj_value, tps_rel_pts_costs, tps_cost = planning.decomp_fit_tps_follow_finger_pts_trajs(self.sim.robot, manip_name,
                                                                              flr2finger_link_names, flr2finger_rel_pts,
                                                                              flr2demo_finger_pts_trajs_rs, init_traj,
                                                                              reg.f,
                                                                              use_collision_cost=self.use_collision_cost,
                                                                              start_fixed=False,
                                                                              alpha=self.alpha, beta_pos=self.beta_pos, gamma=self.gamma, plotting=plotting)

        full_traj = (test_traj, sim_util.dof_inds_from_name(self.sim.robot, manip_name))
        test_aug_traj = demonstration.AugmentedTrajectory.create_from_full_traj(self.sim.robot, full_traj, lr2open_finger_traj=demo_aug_traj_rs.lr2open_finger_traj, lr2close_finger_traj=demo_aug_traj_rs.lr2close_finger_traj)

        if plotting:
            for lr in active_lr:
                flr2new_transformed_finger_pts_traj_rs = {}
                for finger_lr in 'lr':
                    flr2new_transformed_finger_pts_traj_rs[finger_lr] = reg.f.transform_points(np.concatenate(flr2demo_finger_pts_traj_rs[finger_lr], axis=0)).reshape((-1,4,3))
                handles.extend(sim_util.draw_finger_pts_traj(self.sim, flr2new_transformed_finger_pts_traj_rs, (0,1,1)))
                handles.append(self.sim.env.drawlinestrip(test_aug_traj.lr2ee_traj[lr][:,:3,3], 2, (0,0,1)))
                flr2test_finger_pts_traj = sim_util.get_finger_pts_traj(self.sim.robot, lr, full_traj)
                handles.extend(sim_util.draw_finger_pts_traj(self.sim, flr2test_finger_pts_traj, (0,0,1)))
            if self.sim.viewer:
              self.sim.viewer.Step()

        return test_aug_traj


class DecompRegistrationAndTrajectoryTransferer(RegistrationAndTrajectoryTransferer):
    def __init__(self, registration_factory, trajectory_transferer,
                 alpha=settings.ALPHA, # alpha not used.
                 beta_pos=settings.BETA_POS, # beta not used, might be used later or to scale lambda
                 gamma=settings.GAMMA,
                 use_collision_cost=settings.USE_COLLISION_COST,
                 init_trajectory_transferer=None):
        super(DecompRegistrationAndTrajectoryTransferer, self).__init__(registration_factory, trajectory_transferer)
        if not isinstance(registration_factory, registration.TpsRpmRegistrationFactory):
            raise NotImplementedError("DecompRegistrationAndTrajectoryTransferer only supports TpsRpmRegistrationFactory")
        if not isinstance(trajectory_transferer, transfer.FingerTrajectoryTransferer):
            raise NotImplementedError("DecompRegistrationAndTrajectoryTransferer only supports FingerTrajectoryTransferer")
        self.sim = trajectory_transferer.sim
        self.alpha = alpha
        self.beta_pos = beta_pos
        self.gamma = gamma
        self.use_collision_cost = use_collision_cost
        self.init_trajectory_transferer = init_trajectory_transferer

    def opttraj_to_augtraj(self, test_traj, manip, lr2open_finger_traj, lr2close_finger_traj):
        full_traj = (test_traj, sim_util.dof_inds_from_name(self.sim.robot, manip_name))
        test_aug_traj = demonstration.AugmentedTrajectory.create_from_full_traj(self.sim.robot, full_traj, lr2open_finger_traj=lr2open_finger_traj, lr2close_finger_traj=lr2close_finger_traj)

    def traj_to_points(self, aug_traj, resampling=False):
        active_lr = "r"
        if resampling:
            _, timesteps_rs = sim_util.unif_resample(np.c_[(1./settings.JOINT_LENGTH_PER_STEP) * np.concatenate([aug_traj.lr2arm_traj[lr] for lr in active_lr], axis=1),
                                                           (1./settings.FINGER_CLOSE_RATE) * np.concatenate([aug_traj.lr2finger_traj[lr] for lr in active_lr], axis=1)],
                                                     1.)
            demo_aug_traj_rs = aug_traj.get_resampled_traj(timesteps_rs)
        else:
            demo_aug_traj_rs = aug_traj

        lr = 'r'
        arm_name = {"rl":"leftarm", "r":"rightarm"}[lr]
        finger_name = "%s_gripper_l_finger_joint"%lr

        flr2demo_finger_pts_traj_rs = sim_util.get_finger_pts_traj(self.sim.robot, lr, (demo_aug_traj_rs.lr2ee_traj[lr], demo_aug_traj_rs.lr2finger_traj[lr]))
        return flr2demo_finger_pts_traj_rs

    def points_to_array(self, flr2demo_finger_pts_traj):
        temp =  np.r_[flr2demo_finger_pts_traj['l'], flr2demo_finger_pts_traj['r']]
        return temp.reshape(temp.shape[0] * temp.shape[1], temp.shape[2])

    def transfer(self, demo, test_scene_state, callback=None, plotting=False):
        reg = self.registration_factory.register(demo, test_scene_state, callback=callback)

        ######## INITIALIZATION ##########

        # Demonstration Trajectory Points
        tau_bd = self.points_to_array(self.traj_to_points(demo.aug_traj, resampling=True))
        # Dual variables
        lambda_bd = np.zeros(tau_bd.shape)
        # TPS Parameters, point clouds, etc.
        (n,d) = reg.f.x_na.shape
        bend_coefs = np.ones(d) * reg.f.bend_coef if np.isscalar(reg.f.bend_coef) else reg.f.bend_coef
        rot_coefs = np.ones(d) * reg.f.rot_coef if np.isscalar(reg.f.rot_coef) else reg.f.rot_coef
        x_na = reg.f.x_na
        y_ng = reg.f.y_ng
        wt_n = reg.f.wt_n


        lambda_bd[:,0] = .001
        # lambda_bd[:,0] = -.0002
        # lambda_bd[:,1] = -.0002
        # lambda_bd[:,1] = .0002
        # lambda_bd[:,2] = -.0002
        # lambda_bd[:,2] = .0002

        (n,d) = reg.f.x_na.shape
        bend_coefs = np.ones(d) * reg.f.bend_coef if np.isscalar(reg.f.bend_coef) else reg.f.bend_coef
        rot_coefs = np.ones(d) * reg.f.rot_coef if np.isscalar(reg.f.rot_coef) else reg.f.rot_coef
        theta, (N, z) = tps.tps_fit_decomp(reg.f.x_na, reg.f.y_ng, bend_coefs, rot_coefs, reg.f.wt_n, tau_bd, lambda_bd, ret_factorization=True)
        reg.f.update(reg.f.x_na, reg.f.y_ng, bend_coefs, rot_coefs, reg.f.wt_n, theta, N=N, z=z)

        warped_points = reg.f.transform_points(tau_bd)
        target_traj = []
        i = 0
        finger_points = []
        #import ipdb; ipdb.set_trace()
        for point in warped_points:
            finger_points.append(point)
            if i % 4 == 3:
                target_traj.append(np.array(finger_points))
                finger_points = []
            i = i+1

        # test_aug_traj = self.trajectory_transferer.transfer(reg, demo, plotting=plotting)

        handles = []
        lr = 'r'
        if plotting:
            demo_cloud = demo.scene_state.cloud
            test_cloud = reg.test_scene_state.cloud
            demo_color = demo.scene_state.color
            test_color = reg.test_scene_state.color
            handles.append(self.sim.env.plot3(demo_cloud[:,:3], 2, demo_color if demo_color is not None else (1,0,0)))
            handles.append(self.sim.env.drawlinestrip(demo.aug_traj.lr2ee_traj[lr][:,:3,3], 2, (1,0,0)))
            # handles.append(self.sim.env.drawlinestrip(warped_points, 2, (0,0,1)))
            handles.extend(sim_util.draw_finger_pts_traj(self.sim, {'r':target_traj}, (0,0,1)))
            # handles.append(self.sim.env.drawlinestrip(test_aug_traj.lr2ee_traj[lr][:,:3,3], 2, (0,0,1)))
            # handles.append(self.sim.env.drawlinestrip(test_aug_traj_rs.lr2ee_traj[lr][:,:3,3], 2, (1,1,0)))
            # transformed_ee_traj_rs = reg.f.transform_hmats(test_aug_traj.lr2ee_traj[lr])
            # handles.append(self.sim.env.drawlinestrip(transformed_ee_traj_rs[:,:3,3], 2, (0,1,0)))
            if self.sim.viewer:
              self.sim.viewer.Step()

            # handles.append(self.sim.env.plot3(test_cloud[:,:3], 2, test_color if test_color is not None else (0,0,1)))
            # if self.sim.viewer:
            #   self.sim.viewer.Step()

        #import ipdb; ipdb.set_trace()

        active_lr = ""
        for lr in 'lr':
            if lr in demo.aug_traj.lr2arm_traj and sim_util.arm_moved(demo.aug_traj.lr2arm_traj[lr]):
                active_lr += lr
        _, timesteps_rs = sim_util.unif_resample(np.c_[(1./settings.JOINT_LENGTH_PER_STEP) * np.concatenate([demo.aug_traj.lr2arm_traj[lr] for lr in active_lr], axis=1),
                                                       (1./settings.FINGER_CLOSE_RATE) * np.concatenate([demo.aug_traj.lr2finger_traj[lr] for lr in active_lr], axis=1)],
                                                 1.)
        demo_aug_traj_rs = demo.aug_traj.get_resampled_traj(timesteps_rs)


        manip_name = ""
        flr2finger_link_names = []
        flr2demo_finger_pts_trajs_rs = []
        init_traj = np.zeros((len(timesteps_rs),0))
        for lr in active_lr:
            arm_name = {"l":"leftarm", "r":"rightarm"}[lr]
            finger_name = "%s_gripper_l_finger_joint"%lr

            if manip_name:
                manip_name += "+"
            manip_name += arm_name + "+" + finger_name

            init_traj = np.c_[init_traj, demo_aug_traj_rs.lr2arm_traj[lr], demo_aug_traj_rs.lr2finger_traj[lr]]

            if plotting:
                handles.append(self.sim.env.drawlinestrip(demo.aug_traj.lr2ee_traj[lr][:,:3,3], 2, (1,0,0)))
                handles.append(self.sim.env.drawlinestrip(demo_aug_traj_rs.lr2ee_traj[lr][:,:3,3], 2, (1,1,0)))
                transformed_ee_traj_rs = reg.f.transform_hmats(demo_aug_traj_rs.lr2ee_traj[lr])
                handles.append(self.sim.env.drawlinestrip(transformed_ee_traj_rs[:,:3,3], 2, (0,1,0)))
                if self.sim.viewer:
                  self.sim.viewer.Step()

            flr2demo_finger_pts_traj_rs = sim_util.get_finger_pts_traj(self.sim.robot, lr, (demo_aug_traj_rs.lr2ee_traj[lr], demo_aug_traj_rs.lr2finger_traj[lr]))
            flr2demo_finger_pts_trajs_rs.append(flr2demo_finger_pts_traj_rs)

            flr2transformed_finger_pts_traj_rs = {}
            flr2finger_link_name = {}
            flr2finger_rel_pts = {}
            for finger_lr in 'lr':
                flr2transformed_finger_pts_traj_rs[finger_lr] = reg.f.transform_points(np.concatenate(flr2demo_finger_pts_traj_rs[finger_lr], axis=0)).reshape((-1,4,3))
                flr2finger_link_name[finger_lr] = "%s_gripper_%s_finger_tip_link"%(lr,finger_lr)
                flr2finger_rel_pts[finger_lr] = sim_util.get_finger_rel_pts(finger_lr)
            flr2finger_link_names.append(flr2finger_link_name)

            if plotting:
                handles.extend(sim_util.draw_finger_pts_traj(self.sim, flr2demo_finger_pts_traj_rs, (1,1,0)))
                handles.extend(sim_util.draw_finger_pts_traj(self.sim, flr2transformed_finger_pts_traj_rs, (0,1,0)))
                if self.sim.viewer:
                  self.sim.viewer.Step()

        if not self.init_trajectory_transferer:
            # modify the shoulder joint angle of init_traj to be the limit (highest arm) because this usually gives a better local optima (but this might not be the right thing to do)
            dof_inds = sim_util.dof_inds_from_name(self.sim.robot, manip_name)
            joint_ind = self.sim.robot.GetJointIndex("%s_shoulder_lift_joint"%lr)
            init_traj[:,dof_inds.index(joint_ind)] = self.sim.robot.GetDOFLimits([joint_ind])[0][0]

        print "planning joint TPS and finger points trajectory following"
        #test_traj, obj_value, tps_rel_pts_costs, tps_cost = planning.decomp_fit_tps_follow_finger_pts_trajs(self.sim.robot, manip_name,
        #                                                                      flr2finger_link_names, flr2finger_rel_pts,
        #                                                                      flr2demo_finger_pts_trajs_rs, init_traj,
        #                                                                      reg.f,
        #                                                                      use_collision_cost=self.use_collision_cost,
        #                                                                      start_fixed=False,
        #                                                                      alpha=self.alpha, beta_pos=self.beta_pos, gamma=self.gamma)
        robot = self.sim.robot
        flr2demo_finger_pts_trajs = flr2demo_finger_pts_trajs_rs
        f = reg.f
        start_fixed = False

        n_steps = init_traj.shape[0]
        dof_inds = sim_util.dof_inds_from_name(robot, manip_name)
        assert init_traj.shape[1] == len(dof_inds)
        for flr2demo_finger_pts_traj in flr2demo_finger_pts_trajs:
            for demo_finger_pts_traj in flr2demo_finger_pts_traj.values():
                assert len(demo_finger_pts_traj)== n_steps
        assert len(flr2finger_link_names) == len(flr2demo_finger_pts_trajs)

        # expand these
        (n,d) = f.x_na.shape
        if f.wt_n is None:
            wt_n = np.ones(n)
        else:
            wt_n = f.wt_n
        if wt_n.ndim == 1:
            wt_n = wt_n[:,None]
        if wt_n.shape[1] == 1:
            wt_n = np.tile(wt_n, (1,d))

        N = f.N
        init_z = f.z

        if start_fixed:
            init_traj = np.r_[robot.GetDOFValues(dof_inds)[None,:], init_traj[1:]]
            sim_util.unwrap_in_place(init_traj, dof_inds)
            init_traj += robot.GetDOFValues(dof_inds) - init_traj[0,:]
        request = {
            "basic_info" : {
                "n_steps" : n_steps,
                "manip" : manip_name,
                "start_fixed" : start_fixed
            },
            "costs" : [
            {
                "type" : "joint_vel",
                "params": {"coeffs" : [self.gamma/(n_steps-1)]}
            },
            ],
            "constraints" : [
            ],
        }
        if self.use_collision_cost:
            request["costs"].append(
                {
                    "type" : "collision",
                    "params" : {
                      "continuous" : True,
                      "coeffs" : [1000],  # penalty coefficients. list of length one is automatically expanded to a list of length n_timesteps
                      "dist_pen" : [0.025]  # robot-obstacle distance that penalty kicks in. expands to length n_timesteps
                    }
                })
        #if joint_vel_limits is not None:
        #    request["constraints"].append(
        #        {
        #            "type" : "joint_vel_limits",
        #            "params": {"vals" : joint_vel_limits,
        #                      "first_step" : 0,
        #                      "last_step" : n_steps-1
        #                      }
        #          })

        # Now that we've made the initial request that is the same every iteration,
        # we make the loop and add on the things that change.

        nu = 0.001
        traj_diff_thresh = 1e-2*lambda_bd.size
        max_iter = 20
        cur_traj = init_traj
        import datetime; print datetime.datetime.now().time()
        for itr in range(max_iter):
          #if itr == 8:
          #  nu = nu/10;
          request_i = copy.deepcopy(request)
          flr2transformed_finger_pts_traj = {}
          # right arm only...
          for finger_lr in 'lr':
            flr2transformed_finger_pts_traj[finger_lr] = f.transform_points(np.concatenate(flr2demo_finger_pts_trajs[0][finger_lr], axis=0)).reshape((-1,4,3))
          flr2transformed_finger_pts_trajs = [flr2transformed_finger_pts_traj]

          request_i["init_info"] = {
                "type":"given_traj",
                "data":[x.tolist() for x in cur_traj],
            }
          # Add lambdas to the trajectory optimization problem
          traj_dim = int(lambda_bd.shape[0]/2)
          for i_step in range(0,traj_dim*2, 4):
            if i_step < traj_dim:
              finger_lr = 'l'
              traj_step = i_step
            else:
              finger_lr = 'r'
              traj_step = i_step - traj_dim
            finger_link_name = flr2finger_link_name[finger_lr]
            finger_rel_pts = flr2finger_rel_pts[finger_lr]
            if start_fixed and traj_step==0: continue
            request_i["costs"].append(
                {"type":"rel_pts_lambdas",
                  "params":{
                    "lambdas":(-lambda_bd[traj_step:traj_step+4,:]).tolist(),
                    "rel_xyzs":finger_rel_pts.tolist(),
                    "link":finger_link_name,
                    "timestep":traj_step/4,
                    "pos_coeffs":[self.beta_pos/n_steps]*4,
                    }
                  })
          #for (flr2finger_link_name, flr2transformed_finger_pts_traj) in zip(flr2finger_link_names, flr2transformed_finger_pts_trajs):
              #for finger_lr, finger_link_name in flr2finger_link_name.items():
                  #finger_rel_pts = flr2finger_rel_pts[finger_lr]
                  #transformed_finger_pts_traj = flr2transformed_finger_pts_traj[finger_lr]
                  #for (i_step, finger_pts) in enumerate(transformed_finger_pts_traj):
                      #if start_fixed and i_step == 0:
                          #continue
                      #request_i["costs"].append(
                          #{"type":"rel_pts",
                          #"params":{
                              #"xyzs":finger_pts.tolist(),
                              #"rel_xyzs":finger_rel_pts.tolist(),
                              #"link":finger_link_name,
                              #"timestep":i_step,
                              #"pos_coeffs":[np.sqrt(self.beta_pos/n_steps)]*4,
                            #}
                          #})
          s_traj = json.dumps(request_i);
          sys.stdout.write('Solving Traj SQP. ')
          sys.stdout.flush()
          with openravepy.RobotStateSaver(robot):
            with util.suppress_stdout():
              prob = trajoptpy.ConstructProblem(s_traj, robot.GetEnv())
              if plotting:
                viewer = trajoptpy.GetViewer(robot.GetEnv())
                trajoptpy.SetInteractive(True)
              result = trajoptpy.OptimizeProblem(prob)
          cur_traj = result.GetTraj()

          ########### PLOT TRAJ TRAJECTORY HERE ############

          print('Solving TPS')
          # Optimize TPS.
          theta, (N, z) = tps.tps_fit_decomp(x_na, y_ng, bend_coefs, rot_coefs, wt_n, tau_bd, -lambda_bd, ret_factorization=True)
          f.update(x_na, y_ng, bend_coefs, rot_coefs, wt_n, theta, N=N, z=z)

          ########## PLOT TPS TRAJECTORY HERE ###############

          # Compute difference between trajectory points.
          trajpts_tps = reg.f.transform_points(tau_bd)
          # Below is probably the same as doing:
          full_traj = (cur_traj, sim_util.dof_inds_from_name(self.sim.robot, manip_name))
          trajpts_traj = self.points_to_array(sim_util.get_finger_pts_traj(self.sim.robot, 'r', full_traj))
          #trajpts_traj = self.points_to_array(self.traj_to_points(self.opttraj_to_augtraj(cur_traj, manip, demo_aug_traj_rs.lr2open_finger_traj, demo_aug_traj_rs.lr2close_finger_traj)))
          traj_diff = trajpts_traj - trajpts_tps;
          abs_traj_diff = sum(sum(abs(traj_diff)))

          print "Absolute diff between traj pts: ", abs_traj_diff, ". Warp cost: ", f.get_objective()
          lambda_bd = lambda_bd - nu * traj_diff

          if abs_traj_diff < traj_diff_thresh:
            print "TRAJECTORIES CONVERGED"
            break

        print 'Done optimizing'

        import datetime; print datetime.datetime.now().time()
        full_traj = (cur_traj, sim_util.dof_inds_from_name(self.sim.robot, manip_name))
        test_aug_traj = demonstration.AugmentedTrajectory.create_from_full_traj(self.sim.robot, full_traj, lr2open_finger_traj=demo_aug_traj_rs.lr2open_finger_traj, lr2close_finger_traj=demo_aug_traj_rs.lr2close_finger_traj)

        if plotting:
            for lr in active_lr:
                flr2new_transformed_finger_pts_traj_rs = {}
                for finger_lr in 'lr':
                    flr2new_transformed_finger_pts_traj_rs[finger_lr] = reg.f.transform_points(np.concatenate(flr2demo_finger_pts_traj_rs[finger_lr], axis=0)).reshape((-1,4,3))
                handles.extend(sim_util.draw_finger_pts_traj(self.sim, flr2new_transformed_finger_pts_traj_rs, (0,1,1)))
                handles.append(self.sim.env.drawlinestrip(test_aug_traj.lr2ee_traj[lr][:,:3,3], 2, (0,0,1)))
                flr2test_finger_pts_traj = sim_util.get_finger_pts_traj(self.sim.robot, lr, full_traj)
                handles.extend(sim_util.draw_finger_pts_traj(self.sim, flr2test_finger_pts_traj, (0,0,1)))
            if self.sim.viewer:
              self.sim.viewer.Step()

        return test_aug_traj
