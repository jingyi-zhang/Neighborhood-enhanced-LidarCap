import numpy as np
import torch


def compute_error_accel(joints_gt, joints_pred, vis=None):
    """
    Computes acceleration error:
        1/(n-2) \sum_{i=1}^{n-1} X_{i-1} - 2X_i + X_{i+1}
    Note that for each frame that is not visible, three entries in the
    acceleration error should be zero'd out.
    Args:
        joints_gt (Nx14x3).
        joints_pred (Nx14x3).
        vis (N).
    Returns:
        error_accel (N-2).
    """
    # (N-2)x14x3
    accel_gt = joints_gt[:-2] - 2 * joints_gt[1:-1] + joints_gt[2:]
    accel_pred = joints_pred[:-2] - 2 * joints_pred[1:-1] + joints_pred[2:]

    normed = np.linalg.norm(accel_pred - accel_gt, axis=2)

    if vis is None:
        new_vis = np.ones(len(normed), dtype=bool)
    else:
        invis = np.logical_not(vis)
        invis1 = np.roll(invis, -1)
        invis2 = np.roll(invis, -2)
        new_invis = np.logical_or(invis, np.logical_or(invis1, invis2))[:-2]
        new_vis = np.logical_not(new_invis)

    return np.mean(normed[new_vis], axis=1)


def batch_compute_similarity_transform_torch(S1, S2):
    '''
    Computes a similarity transform (sR, t) that takes
    a set of 3D points S1 (3 x N) closest to a set of 3D points S2,
    where R is an 3x3 rotation matrix, t 3x1 translation, s scale.
    i.e. solves the orthogonal Procrutes problem.
    '''
    transposed = False
    if S1.shape[0] != 3 and S1.shape[0] != 2:
        S1 = S1.permute(0, 2, 1)
        S2 = S2.permute(0, 2, 1)
        transposed = True
    assert(S2.shape[1] == S1.shape[1])

    # 1. Remove mean.
    mu1 = S1.mean(axis=-1, keepdims=True)
    mu2 = S2.mean(axis=-1, keepdims=True)

    X1 = S1 - mu1
    X2 = S2 - mu2

    # 2. Compute variance of X1 used for scale.
    var1 = torch.sum(X1**2, dim=1).sum(dim=1)

    # 3. The outer product of X1 and X2.
    K = X1.bmm(X2.permute(0, 2, 1))

    # 4. Solution that Maximizes trace(R'K) is R=U*V', where U, V are
    # singular vectors of K.
    U, s, V = torch.svd(K)

    # Construct Z that fixes the orientation of R to get det(R)=1.
    Z = torch.eye(U.shape[1], device=S1.device).unsqueeze(0)
    Z = Z.repeat(U.shape[0], 1, 1)
    Z[:, -1, -1] *= torch.sign(torch.det(U.bmm(V.permute(0, 2, 1))))

    # Construct R.
    R = V.bmm(Z.bmm(U.permute(0, 2, 1)))

    # 5. Recover scale.
    scale = torch.cat([torch.trace(x).unsqueeze(0) for x in R.bmm(K)]) / var1

    # 6. Recover translation.
    t = mu2 - (scale.unsqueeze(-1).unsqueeze(-1) * (R.bmm(mu1)))

    # 7. Error:
    S1_hat = scale.unsqueeze(-1).unsqueeze(-1) * R.bmm(S1) + t

    if transposed:
        S1_hat = S1_hat.permute(0, 2, 1)

    return S1_hat


def compute_errors(gt3ds, preds):
    """
    Gets MPJPE after pelvis alignment + MPJPE after Procrustes.
    Evaluates on the 24 common joints.
    Inputs:
      - gt3ds: N x 24 x 3
      - preds: N x 24 x 3
    """
    errors, errors_pa = [], []
    errors = np.sqrt(((preds - gt3ds) ** 2).sum(-1)).mean(-1)
    S1_hat = batch_compute_similarity_transform_torch(
        torch.from_numpy(preds).float(), torch.from_numpy(gt3ds).float()).numpy()
    errors_pa = np.sqrt(((S1_hat - gt3ds) ** 2).sum(-1)).mean(-1)
    return errors, errors_pa


torsal_length = 0.5127067


def compute_pck(pred_joints, gt_joints, threshold):
    # (B, N, 3), (B, N, 3)
    B = len(pred_joints)
    pred_joints -= pred_joints[:, :1, :]
    gt_joints -= gt_joints[:, :1, :]

    distance = np.sqrt(((pred_joints - gt_joints) ** 2).sum(-1))  # (B, N)
    correct = distance < threshold * torsal_length
    correct = (correct.sum(0) / B).mean()
    return correct


def output_metric(pred_joints, gt_joints):
    #assert pred_joints.shape[1] == 24 and pred_joints.shape[2] == 3

    m2mm = 1000

    pred_joints -= pred_joints[:, :1, :]
    gt_joints -= gt_joints[:, :1, :]

    accel_error = np.mean(compute_error_accel(gt_joints, pred_joints)) * m2mm
    mpjpe, pa_mpjpe = compute_errors(gt_joints, pred_joints)

    mpjpe = np.mean(mpjpe) * m2mm
    pa_mpjpe = np.mean(pa_mpjpe) * m2mm

    pck_30 = compute_pck(pred_joints, gt_joints, 0.3)
    pck_50 = compute_pck(pred_joints, gt_joints, 0.5)

    return {'mpjpe': mpjpe, 'pa_mpjpe': pa_mpjpe, 'pck_30': pck_30, 'pck_50': pck_50, 'accel_error': accel_error}
