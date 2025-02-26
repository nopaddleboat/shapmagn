"""
PointPWC-Net model and losses
Author: Wenxuan Wu
Date: May 2020
"""
from functools import partial
import torch.nn as nn
import torch
from pykeops.torch import LazyTensor
import numpy as np
import torch.nn.functional as F
from shapmagn.modules_reg.networks.pointconv_util import (
    PointConv,
    PointConvD,
    PointWarping2,
    UpsampleFlow2,
    PointWarping,
    UpsampleFlow,
    PointConvFlow,
    SceneFlowEstimatorPointConv,
)
from shapmagn.modules_reg.networks.pointconv_util import (
    index_points_gather as index_points,
    index_points_group,
    Conv1d,
    square_distance,
)
import time
from shapmagn.utils.utils import shrink_by_factor
from shapmagn.shape.point_interpolator import nadwat_kernel_interpolator

scale = 1.0


class PointConvSceneFlowPWC(nn.Module):
    def __init__(
        self,
        input_channel=3,
        initial_radius=1.0,
        initial_npoints=2048,
        predict_at_low_resl=False,
        param_shrink_factor=1,
    ):
        super(PointConvSceneFlowPWC, self).__init__()

        flow_nei = 32
        feat_nei = 16
        sbf = partial(shrink_by_factor, factor=param_shrink_factor)
        self.predict_at_low_resl = predict_at_low_resl
        self.scale = scale
        # l0: 8192
        self.level0 = Conv1d(input_channel, sbf(32))
        self.level0_1 = Conv1d(sbf(32), sbf(32))
        self.cost0 = PointConvFlow(flow_nei, sbf(32 + 32 + 32 + 32) + 3, sbf([32, 32]))
        self.flow0 = SceneFlowEstimatorPointConv(sbf(32 + 64), sbf(32))
        self.level0_2 = Conv1d(sbf(32), sbf(64))

        # l1: 2048
        self.level1 = PointConvD(initial_npoints, feat_nei, sbf(64 + 3), sbf(64))
        self.cost1 = PointConvFlow(flow_nei, sbf(64 + 32 + 64 + 32) + 3, sbf([64, 64]))
        self.flow1 = SceneFlowEstimatorPointConv(sbf(64 + 64), sbf(64))
        self.level1_0 = Conv1d(sbf(64), sbf(64))
        self.level1_1 = Conv1d(sbf(64), sbf(128))

        # l2: 512
        self.level2 = PointConvD(
            int(initial_npoints / 4), feat_nei, sbf(128) + 3, sbf(128)
        )
        self.cost2 = PointConvFlow(
            flow_nei, sbf(128 + 64 + 128 + 64) + 3, sbf([128, 128])
        )
        self.flow2 = SceneFlowEstimatorPointConv(sbf(128 + 64), sbf(128))
        self.level2_0 = Conv1d(sbf(128), sbf(128))
        self.level2_1 = Conv1d(sbf(128), sbf(256))

        # l3: 256
        self.level3 = PointConvD(
            int(initial_npoints / 8), feat_nei, sbf(256) + 3, sbf(256)
        )
        self.cost3 = PointConvFlow(
            flow_nei, sbf(256 + 64 + 256 + 64) + 3, sbf([256, 256])
        )
        self.flow3 = SceneFlowEstimatorPointConv(sbf(256), sbf(256), flow_ch=0)
        self.level3_0 = Conv1d(sbf(256), sbf(256))
        self.level3_1 = Conv1d(sbf(256), sbf(512))

        # l4: 64
        self.level4 = PointConvD(
            int(initial_npoints / 32), feat_nei, sbf(512) + 3, sbf(256)
        )

        # deconv
        self.deconv4_3 = Conv1d(sbf(256), sbf(64))
        self.deconv3_2 = Conv1d(sbf(256), sbf(64))
        self.deconv2_1 = Conv1d(sbf(128), sbf(32))
        self.deconv1_0 = Conv1d(sbf(64), sbf(32))

        # warping
        self.warping = PointWarping2(initial_radius)

        # upsample
        self.upsample = UpsampleFlow2(initial_radius)

    def forward(self, xyz1, xyz2, color1, color2):
        # xyz1, xyz2: B, N, 3
        # color1, color2: B, N, 3

        # l0
        scale = 2 ** (1 / 3)
        pc1_l0 = xyz1.permute(0, 2, 1)
        pc2_l0 = xyz2.permute(0, 2, 1)
        color1 = color1.permute(0, 2, 1)  # B 3 N
        color2 = color2.permute(0, 2, 1)  # B 3 N
        feat1_l0 = self.level0(color1)
        feat1_l0 = self.level0_1(feat1_l0)
        feat1_l0_1 = self.level0_2(feat1_l0)

        feat2_l0 = self.level0(color2)
        feat2_l0 = self.level0_1(feat2_l0)
        feat2_l0_1 = self.level0_2(feat2_l0)

        # l1
        pc1_l1, feat1_l1, fps_pc1_l1 = self.level1(pc1_l0, feat1_l0_1)
        feat1_l1_2 = self.level1_0(feat1_l1)
        feat1_l1_2 = self.level1_1(feat1_l1_2)

        pc2_l1, feat2_l1, fps_pc2_l1 = self.level1(pc2_l0, feat2_l0_1)
        feat2_l1_2 = self.level1_0(feat2_l1)
        feat2_l1_2 = self.level1_1(feat2_l1_2)

        # l2
        pc1_l2, feat1_l2, fps_pc1_l2 = self.level2(pc1_l1, feat1_l1_2)
        feat1_l2_3 = self.level2_0(feat1_l2)
        feat1_l2_3 = self.level2_1(feat1_l2_3)

        pc2_l2, feat2_l2, fps_pc2_l2 = self.level2(pc2_l1, feat2_l1_2)
        feat2_l2_3 = self.level2_0(feat2_l2)
        feat2_l2_3 = self.level2_1(feat2_l2_3)

        # l3
        pc1_l3, feat1_l3, fps_pc1_l3 = self.level3(pc1_l2, feat1_l2_3)
        feat1_l3_4 = self.level3_0(feat1_l3)
        feat1_l3_4 = self.level3_1(feat1_l3_4)

        pc2_l3, feat2_l3, fps_pc2_l3 = self.level3(pc2_l2, feat2_l2_3)
        feat2_l3_4 = self.level3_0(feat2_l3)
        feat2_l3_4 = self.level3_1(feat2_l3_4)

        # l4
        pc1_l4, feat1_l4, _ = self.level4(pc1_l3, feat1_l3_4)
        feat1_l4_3 = self.upsample(pc1_l3, pc1_l4, feat1_l4, resol_factor=scale ** 7)
        feat1_l4_3 = self.deconv4_3(feat1_l4_3)

        pc2_l4, feat2_l4, _ = self.level4(pc2_l3, feat2_l3_4)
        feat2_l4_3 = self.upsample(pc2_l3, pc2_l4, feat2_l4, resol_factor=scale ** 7)
        feat2_l4_3 = self.deconv4_3(feat2_l4_3)

        # l3
        c_feat1_l3 = torch.cat([feat1_l3, feat1_l4_3], dim=1)
        c_feat2_l3 = torch.cat([feat2_l3, feat2_l4_3], dim=1)
        cost3 = self.cost3(pc1_l3, pc2_l3, c_feat1_l3, c_feat2_l3)
        feat3, resid_flow3 = self.flow3(pc1_l3, feat1_l3, cost3)
        flow3 = resid_flow3

        feat1_l3_2 = self.upsample(pc1_l2, pc1_l3, feat1_l3, resol_factor=scale ** 5)
        feat1_l3_2 = self.deconv3_2(feat1_l3_2)

        feat2_l3_2 = self.upsample(pc2_l2, pc2_l3, feat2_l3, resol_factor=scale ** 5)
        feat2_l3_2 = self.deconv3_2(feat2_l3_2)

        c_feat1_l2 = torch.cat([feat1_l2, feat1_l3_2], dim=1)
        c_feat2_l2 = torch.cat([feat2_l2, feat2_l3_2], dim=1)

        feat1_l2_1 = self.upsample(pc1_l1, pc1_l2, feat1_l2, resol_factor=scale ** 4)
        feat1_l2_1 = self.deconv2_1(feat1_l2_1)

        feat2_l2_1 = self.upsample(pc2_l1, pc2_l2, feat2_l2, resol_factor=scale ** 4)
        feat2_l2_1 = self.deconv2_1(feat2_l2_1)

        c_feat1_l1 = torch.cat([feat1_l1, feat1_l2_1], dim=1)
        c_feat2_l1 = torch.cat([feat2_l1, feat2_l2_1], dim=1)

        feat1_l1_0 = self.upsample(pc1_l0, pc1_l1, feat1_l1, resol_factor=scale ** 3)
        feat1_l1_0 = self.deconv1_0(feat1_l1_0)

        feat2_l1_0 = self.upsample(pc2_l0, pc2_l1, feat2_l1, resol_factor=scale ** 3)
        feat2_l1_0 = self.deconv1_0(feat2_l1_0)

        c_feat1_l0 = torch.cat([feat1_l0, feat1_l1_0], dim=1)
        c_feat2_l0 = torch.cat([feat2_l0, feat2_l1_0], dim=1)

        # l2
        up_flow2 = self.upsample(
            pc1_l2, pc1_l3, self.scale * flow3, resol_factor=scale ** 5
        )
        pc2_l2_warp = self.warping(pc1_l2, pc2_l2, up_flow2, resol_factor=scale ** 4)
        cost2 = self.cost2(pc1_l2, pc2_l2_warp, c_feat1_l2, c_feat2_l2)

        feat3_up = self.upsample(pc1_l2, pc1_l3, feat3, resol_factor=scale ** 5)
        new_feat1_l2 = torch.cat([feat1_l2, feat3_up], dim=1)
        feat2, resid_flow2 = self.flow2(pc1_l2, new_feat1_l2, cost2, up_flow2)
        flow2 = up_flow2 + resid_flow2

        # l1
        up_flow1 = self.upsample(
            pc1_l1, pc1_l2, self.scale * flow2, resol_factor=scale ** 4
        )
        pc2_l1_warp = self.warping(pc1_l1, pc2_l1, up_flow1, resol_factor=scale ** 3)
        cost1 = self.cost1(pc1_l1, pc2_l1_warp, c_feat1_l1, c_feat2_l1)

        feat2_up = self.upsample(pc1_l1, pc1_l2, feat2, resol_factor=scale ** 4)
        new_feat1_l1 = torch.cat([feat1_l1, feat2_up], dim=1)
        feat1, resid_flow1 = self.flow1(pc1_l1, new_feat1_l1, cost1, up_flow1)
        flow1 = up_flow1 + resid_flow1
        if not self.predict_at_low_resl:
            # l0
            up_flow0 = self.upsample(
                pc1_l0, pc1_l1, self.scale * flow1, resol_factor=scale ** 3
            )  # todo interpolate the resid flow and then add it with the interoplated prev flow
            pc2_l0_warp = self.warping(pc1_l0, pc2_l0, up_flow0, resol_factor=1)
            cost0 = self.cost0(pc1_l0, pc2_l0_warp, c_feat1_l0, c_feat2_l0)

            feat1_up = self.upsample(pc1_l0, pc1_l1, feat1, resol_factor=scale ** 3)
            new_feat1_l0 = torch.cat([feat1_l0, feat1_up], dim=1)
            _, resid_flow0 = self.flow0(pc1_l0, new_feat1_l0, cost0, up_flow0)
            flow0 = up_flow0 + resid_flow0
            floweds = [
                flow0 + pc1_l0.detach(),
                flow1 + pc1_l1.detach(),
                flow2 + pc1_l2.detach(),
                flow3 + pc1_l3.detach(),
            ]
            floweds = [flow.transpose(2, 1).contiguous() for flow in floweds]
            fps_pc1_idxs = [fps_pc1_l1, fps_pc1_l2, fps_pc1_l3]
            additional_param = {
                "control_points": pc1_l1,
                "control_points_idx": fps_pc1_l1,
                "predict_at_low_resl": self.predict_at_low_resl,
            }

            return flow0.transpose(2, 1).contiguous(), additional_param.update(
                {"floweds": floweds, "fps_pc1_idxs": fps_pc1_idxs}
            )
        else:
            floweds = [
                flow1 + pc1_l1.detach(),
                flow2 + pc1_l2.detach(),
                flow3 + pc1_l3.detach(),
            ]
            floweds = [flow.transpose(2, 1).contiguous() for flow in floweds]
            fps_pc1_idxs = [fps_pc1_l1, fps_pc1_l2, fps_pc1_l3]
            additional_param = {
                "control_points": pc1_l1,
                "control_points_idx": fps_pc1_l1,
                "predict_at_low_resl": self.predict_at_low_resl,
            }

            return flow1.transpose(2, 1).contiguous(), additional_param.update(
                {"floweds": floweds, "fps_pc1_idxs": fps_pc1_idxs}
            )


def multiScaleLoss(pred_flows, gt_flow, fps_idxs, alpha=[0.02, 0.04, 0.08, 0.16]):
    # num of scale
    num_scale = len(pred_flows)
    offset = len(fps_idxs) - num_scale + 1

    # generate GT list and mask1s
    gt_flows = [gt_flow]
    for i in range(1, len(fps_idxs) + 1):
        fps_idx = fps_idxs[i - 1]
        sub_gt_flow = index_points(gt_flows[-1], fps_idx) / scale
        gt_flows.append(sub_gt_flow)

    total_loss = torch.zeros(1).cuda()
    for i in range(num_scale):
        diff_flow = pred_flows[i].permute(0, 2, 1) - gt_flows[i + offset]
        total_loss += alpha[i] * torch.norm(diff_flow, dim=2).sum(dim=1).mean()

    return total_loss


def curvature(pc):
    # pc: B 3 N
    pc = pc.permute(0, 2, 1)
    sqrdist = square_distance(pc, pc)
    _, kidx = torch.topk(sqrdist, 10, dim=-1, largest=False, sorted=False)  # B N 10 3
    grouped_pc = index_points_group(pc, kidx)
    pc_curvature = torch.sum(grouped_pc - pc.unsqueeze(2), dim=2) / 9.0
    return pc_curvature  # B N 3


def computeChamfer(pc1, pc2):
    """
    pc1: B 3 N
    pc2: B 3 M
    """
    pc1 = pc1.permute(0, 2, 1)
    pc2 = pc2.permute(0, 2, 1)
    sqrdist12 = square_distance(pc1, pc2)  # B N M

    # chamferDist
    dist1, _ = torch.topk(sqrdist12, 1, dim=-1, largest=False, sorted=False)
    dist2, _ = torch.topk(sqrdist12, 1, dim=1, largest=False, sorted=False)
    dist1 = dist1.squeeze(2)
    dist2 = dist2.squeeze(1)

    return dist1, dist2


def computeChamfer2(pc1, pc2):
    """
    pc1: B 3 N
    pc2: B 3 M
    """
    pc1 = pc1.permute(0, 2, 1)
    pc2 = pc2.permute(0, 2, 1)
    pc_i, pc_j = LazyTensor(pc1[:, None, :]), LazyTensor(pc2[None, :, :])
    sqdist12 = pc_i.sqdist(pc_j)
    dist1, _ = sqdist12.argmin(dim=2)
    dist2, _ = sqdist12.argmin(dim=1)
    return dist1, dist2


def curvatureWarp2(pc, warped_pc, K=9):
    """

    :param pc:  BxNxD
    :return:
    """
    warped_pc = warped_pc.permute(0, 2, 1).contiguous()
    pc = pc.permute(0, 2, 1).contiguous()
    B, N, D = pc.shape[0], pc.shape[1], pc.shape[2]
    pc_i, pc_j = LazyTensor(pc[:, None, :]), LazyTensor(pc[None, :, :])
    _, kidx = pc_i.sqdist(pc_j).argKmin(K=K, dim=2).long().view(B, N, K)
    grouped_pc = index_points_group(warped_pc, kidx)
    pc_curvature = torch.sum(grouped_pc - warped_pc.unsqueeze(2), dim=2) / 9.0
    return pc_curvature  # B N 3


def curvatureWarp(pc, warped_pc):
    """

    :param pc: BxDxN
    :param warped_pc:  BxDxM
    :return:
    """

    warped_pc = warped_pc.permute(0, 2, 1).contiguous()
    pc = pc.permute(0, 2, 1).contiguous()
    sqrdist = square_distance(pc, pc)
    _, kidx = torch.topk(sqrdist, 10, dim=-1, largest=False, sorted=False)  # B N 10 3
    grouped_pc = index_points_group(warped_pc, kidx)
    pc_curvature = torch.sum(grouped_pc - warped_pc.unsqueeze(2), dim=2) / 9.0
    return pc_curvature  # B N 3


def computeSmooth2(pc1, pred_flow, K=9):
    """
    pc1: B 3 N
    pred_flow: B 3 N
    """

    pc1 = pc1.permute(0, 2, 1)
    pred_flow = pred_flow.permute(0, 2, 1)
    pc_i, pc_j = LazyTensor(pc1[:, None, :]), LazyTensor(1[None, :, :])
    _, kidx = pc_i.sqdist(pc_j).argKmin(K=K, dim=2).long().view(B, N, K)
    grouped_flow = index_points_group(pred_flow, kidx)  # B N 9 3
    diff_flow = torch.norm(grouped_flow - pred_flow.unsqueeze(2), dim=3).sum(dim=2) / (
        K - 1
    )

    return diff_flow


def computeSmooth(pc1, pred_flow):
    """
    pc1: B 3 N
    pred_flow: B 3 N
    """

    pc1 = pc1.permute(0, 2, 1)
    pred_flow = pred_flow.permute(0, 2, 1)
    sqrdist = square_distance(pc1, pc1)  # B N N

    # Smoothness
    _, kidx = torch.topk(sqrdist, 9, dim=-1, largest=False, sorted=False)
    grouped_flow = index_points_group(pred_flow, kidx)  # B N 9 3
    diff_flow = (
        torch.norm(grouped_flow - pred_flow.unsqueeze(2), dim=3).sum(dim=2) / 8.0
    )

    return diff_flow


def interpolateCurvature2(pc1, pc2, pc2_curvature, K=5):
    """
    pc1: B D N
    pc2: B D M
    pc2_curvature: B D M
    """

    pc1 = pc1.permute(0, 2, 1)
    pc2 = pc2.permute(0, 2, 1)
    pc2_curvature = pc2_curvature
    interpolator = nadwat_kernel_interpolator(scale=0.1)
    pc1_interp = interpolator(pc1, pc2, pc2_curvature)
    return pc1_interp.permute(0, 2, 1)


def interpolateCurvature(pc1, pc2, pc2_curvature):
    """
    pc1: B 3 N
    pc2: B 3 M
    pc2_curvature: B 3 M
    """

    B, _, N = pc1.shape
    pc1 = pc1.permute(0, 2, 1)
    pc2 = pc2.permute(0, 2, 1)
    pc2_curvature = pc2_curvature

    sqrdist12 = square_distance(pc1, pc2)  # B N M
    dist, knn_idx = torch.topk(sqrdist12, 5, dim=-1, largest=False, sorted=False)
    grouped_pc2_curvature = index_points_group(pc2_curvature, knn_idx)  # B N 5 3
    norm = torch.sum(1.0 / (dist + 1e-8), dim=2, keepdim=True)
    weight = (1.0 / (dist + 1e-8)) / norm

    inter_pc2_curvature = torch.sum(
        weight.view(B, N, 5, 1) * grouped_pc2_curvature, dim=2
    )
    return inter_pc2_curvature


def multiScaleChamferSmoothCurvature(pc1, pc2, pred_flows):
    f_curvature = 0.3
    f_smoothness = 1.0
    f_chamfer = 1.0

    # num of scale
    num_scale = len(pred_flows)

    alpha = [0.02, 0.04, 0.08, 0.16]
    chamfer_loss = torch.zeros(1).cuda()
    smoothness_loss = torch.zeros(1).cuda()
    curvature_loss = torch.zeros(1).cuda()
    for i in range(num_scale):
        cur_pc1 = pc1[i]  # B 3 N
        cur_pc2 = pc2[i]
        cur_flow = pred_flows[i]  # B 3 N

        # compute curvature
        cur_pc2_curvature = curvature(cur_pc2)

        cur_pc1_warp = cur_pc1 + cur_flow
        dist1, dist2 = computeChamfer(cur_pc1_warp, cur_pc2)
        moved_pc1_curvature = curvatureWarp(cur_pc1, cur_pc1_warp)

        chamferLoss = dist1.sum(dim=1).mean() + dist2.sum(dim=1).mean()

        # smoothness
        smoothnessLoss = computeSmooth(cur_pc1, cur_flow).sum(dim=1).mean()

        # curvature
        inter_pc2_curvature = interpolateCurvature(
            cur_pc1_warp, cur_pc2, cur_pc2_curvature
        )
        curvatureLoss = (
            torch.sum((inter_pc2_curvature - moved_pc1_curvature) ** 2, dim=2)
            .sum(dim=1)
            .mean()
        )

        chamfer_loss += alpha[i] * chamferLoss
        smoothness_loss += alpha[i] * smoothnessLoss
        curvature_loss += alpha[i] * curvatureLoss

    total_loss = (
        f_chamfer * chamfer_loss
        + f_curvature * curvature_loss
        + f_smoothness * smoothness_loss
    )

    return total_loss, chamfer_loss, curvature_loss, smoothness_loss


if __name__ == "__main__":

    import time

    num_points = 8192
    xyz1 = torch.rand(1, num_points, 3).cuda()
    xyz2 = torch.rand(1, num_points, 3).cuda()
    color1 = torch.rand(1, num_points, 3).cuda()
    color2 = torch.rand(1, num_points, 3).cuda()

    gt_flow = torch.rand(1, num_points, 3).cuda()
    mask1 = torch.ones(1, num_points, dtype=torch.bool).cuda()
    model = PointConvSceneFlowPWC8192selfglobalPointConv().cuda()

    model.eval()
    for _ in range(1):
        with torch.no_grad():
            flows, fps_pc1_idxs, fps_pc2_idxs, pc1, pc2 = model(
                xyz1, xyz2, color1, color2
            )
            torch.cuda.synchronize()

    loss = multiScaleLoss(flows, gt_flow, fps_pc1_idxs)

    self_loss = multiScaleChamferSmoothCurvature(pc1, pc2, flows)

    print(flows[0].shape, loss)
    print(self_loss)
