import numpy as np
import importlib
from itertools import combinations

from structures import AccuracyTestParameters
from src.board_builder.board_builder import BoardBuilder
from src.common.util import register_corresponding_points
from src.common.structures import \
    MarkerCornerImagePoint, \
    MarkerSnapshot, \
    TargetBoard, \
    Marker
from utils import \
    generate_virtual_snapshots, \
    generate_data, \
    graph_renderer


class AccuracyTest:
    def __init__(self):
        self._parameters = AccuracyTestParameters
        self.board_builder_with_noise = BoardBuilder(self._parameters.BOARD_MARKER_SIZE)
        self.board_builder = BoardBuilder(self._parameters.BOARD_MARKER_SIZE)
        self.board_builder.pose_solver.set_board_marker_size(self._parameters.BOARD_MARKER_SIZE)

    def _add_noise_to_corners(self, data):
        """
        Adds a limited amount of noise to a percentage of data within two standard deviations and adds more noise to
        the remainder. Noise is added to both the x and y coordinates.
        """
        noisy_data = {}
        noise_mean = 0
        noise_std_dev = self._parameters.NOISE_LEVEL / 2  # Two standard deviations should be within NOISE_LEVEL

        for detector_name, marker_snapshots in data.items():
            noisy_marker_snapshots = []
            for marker_snapshot in marker_snapshots:
                noisy_corners = []
                total_points = len(marker_snapshot.corner_image_points)

                # Generate noise for all points, clamped to 2-3 standard deviations
                # We multiply the size by two because noise is added to both x and y coordinates
                noise = np.clip(
                    np.random.normal(noise_mean, noise_std_dev, total_points * 2),
                    -self._parameters.NOISE_LEVEL * self._parameters.HIGH_NOISE_LEVEL,
                    self._parameters.NOISE_LEVEL * self._parameters.HIGH_NOISE_LEVEL
                )

                # Apply noise
                for i, corner in enumerate(marker_snapshot.corner_image_points):
                    noisy_corner_x = corner.x_px + noise[i * 2]
                    noisy_corner_y = corner.y_px + noise[i * 2 + 1]
                    noisy_corners.append(MarkerCornerImagePoint(x_px=noisy_corner_x, y_px=noisy_corner_y))

                noisy_marker_snapshot = MarkerSnapshot(label=marker_snapshot.label, corner_image_points=noisy_corners)
                noisy_marker_snapshots.append(noisy_marker_snapshot)

            noisy_data[detector_name] = noisy_marker_snapshots
        return noisy_data

    @staticmethod
    def align_boards(target_board: TargetBoard, simulated_board: TargetBoard) -> TargetBoard:
        def transform_point(point, matrix):
            """Applies a 4x4 transformation matrix to a 3D point."""
            point_h = np.array([point[0], point[1], point[2], 1.0])  # convert to homogeneous coordinates
            transformed_point_h = np.matmul(matrix, point_h)
            return transformed_point_h[:3]  # convert back to 3D coordinates

        # Extract points from the boards
        target_points: list[list[float]] = target_board.get_points()
        simulated_points: list[list[float]] = simulated_board.get_points()

        # Get the transformation matrix
        transformation_matrix = register_corresponding_points(target_points, simulated_points)

        # Apply the transformation to all markers in the target_board
        aligned_markers = []
        for marker in target_board.markers:
            aligned_points = [transform_point(point, transformation_matrix) for point in marker.points]
            for i in range(len(aligned_points)):
                aligned_points[i] = list(aligned_points[i])
            aligned_markers.append(
                Marker(marker_id=marker.marker_id, marker_size=marker.marker_size, points=aligned_points))

        # Return the aligned TargetBoard
        return TargetBoard(target_id=target_board.target_id, markers=aligned_markers)

    @staticmethod
    def _calculate_generate_snapshots_inaccuracy(
            board_definition: dict[int, list[list[float]]],
            snapshots: list[dict[int, list[list[float]]]]
    ) -> float:
        """
        Validation method that calculates the difference between the generated board and the original input board
        """
        def compare_transforms(T1: np.ndarray, T2: np.ndarray) -> float:
            return np.linalg.norm(T1 - T2)

        def calculate_relative_transform(marker0_points: np.ndarray, marker1_points: np.ndarray):
            def calculate_transform(points: np.ndarray) -> np.ndarray:
                center = np.mean(points, axis=0)
                v1 = points[1] - points[0]
                v2 = points[3] - points[0]
                normal = np.cross(v1, v2)
                x_axis = v1 / np.linalg.norm(v1)
                z_axis = normal / np.linalg.norm(normal)
                y_axis = np.cross(z_axis, x_axis)
                rotation = np.column_stack((x_axis, y_axis, z_axis))
                transform = np.eye(4)
                transform[:3, :3] = rotation
                transform[:3, 3] = center
                return transform

            T0 = calculate_transform(marker0_points)
            T1 = calculate_transform(marker1_points)
            T0_to_T1 = np.linalg.inv(T0) @ T1
            return T0_to_T1

        theoretical_transforms = {}
        for (i, j) in combinations(board_definition.keys(), 2):
            theoretical_transforms[(i, j)] = calculate_relative_transform(
                np.array(board_definition[i]), np.array(board_definition[j]))

        # Calculate and compare relative transforms for each snapshot
        total_difference = 0
        num_comparisons = 0
        for snapshot in snapshots:
            for (i, j) in combinations(snapshot.keys(), 2):
                snapshot_transform = calculate_relative_transform(
                    np.array(snapshot[i]), np.array(snapshot[j]))
                difference = compare_transforms(theoretical_transforms[(i, j)], snapshot_transform)
                total_difference += difference
                num_comparisons += 1

        average_difference = total_difference / num_comparisons

        return average_difference

    @staticmethod
    def _calculate_rms_error_of_two_corner_dataset(
            simulated_board_arrangement: TargetBoard,
            algorithm_board_arrangement: TargetBoard
    ) -> float:
        """
        Calculates the RMS difference between the output board arrangement with the input board arrangement
        """

        simulated_data: list[list[float]] = simulated_board_arrangement.get_points()
        algorithm_data: list[list[float]] = algorithm_board_arrangement.get_points()

        if len(simulated_data) != len(algorithm_data):
            raise ValueError("Theoretical and experimental data must have the same length.")

        rms_error = 0.0
        total_points = 0

        # Simulated data and algorithm data are both sorted in increasing id order
        for arr1, arr2 in zip(simulated_data, algorithm_data):
            if len(arr1) != len(arr2):
                raise ValueError("Corresponding arrays must have the same length.")
            diff = np.array(arr1) - np.array(arr2)
            rms_error += np.mean(diff ** 2)
            total_points += 1

        rms_error = np.sqrt(rms_error / total_points)
        return rms_error

    def run_accuracy_tester(self):
        module = importlib.import_module(f"src.board_builder.test.accuracy.board_definitions.{self._parameters.SCENE_NAME}")
        board_definition = getattr(module, "BOARD_DEFINITION")

        ### REFERENCE DATA ###
        for pose in self._parameters.DETECTOR_POSES_IN_WORLD_REFERENCE:
            self.board_builder.pose_solver.set_intrinsic_parameters(pose.target_id, self._parameters.DETECTOR_INTRINSICS)

        self.board_builder.pose_solver.set_detector_poses(self._parameters.DETECTOR_POSES_IN_WORLD_REFERENCE)

        ### GENERATE SNAPSHOTS OF THE SCENE ###
        snapshots = generate_virtual_snapshots(board_definition, self._parameters.NUMBER_OF_SNAPSHOTS)
        average_difference = self._calculate_generate_snapshots_inaccuracy(board_definition, snapshots)
        print(f"Generated snapshots inaccuracy (average Frobenius norm difference): {average_difference}")

        ### COLLECTION DATA ###
        two_dimension_collection_data = []

        for snapshot in snapshots:
            collection_data = generate_data(snapshot, self._parameters.DETECTOR_POSES_IN_WORLD_REFERENCE, remove_markers_out_of_frame=True)
            two_dimension_collection_data.append(collection_data)
            noisy_collection_data = self._add_noise_to_corners(collection_data)
            # TODO: The predicted target pose seems to have an offset (around 6 mm) in the x direction of the world
            #  reference system. This might be a problem in either the projection (input data), pose solver
            #  (algorithm), or detector intrinsics (calibration)
            self.board_builder.collect_data(noisy_collection_data)
        #graph_renderer(snapshots, two_dimension_collection_data, self._parameters.DETECTOR_POSES_IN_WORLD_REFERENCE)

        ### BUILD BOARD ###
        predicted_board = self.board_builder.build_board()

        # Center the board definition around the reference marker
        board_definition_np = {k: np.array(v) for k, v in board_definition.items()}
        marker_0_corners = board_definition_np[0]
        center_marker_0 = np.mean(marker_0_corners, axis=0)
        simulated_board_definition = {str(k): v - center_marker_0 for k, v in board_definition_np.items()}

        simulated_markers = [
            Marker(marker_id=k, points=v.tolist())
            for k, v in sorted(simulated_board_definition.items())
        ]
        simulated_board = TargetBoard(target_id='simulated_board', markers=simulated_markers)

        # RMS
        if not predicted_board or not simulated_board:
            return None
        aligned_board = self.align_boards(predicted_board, simulated_board)
        rms_error = self._calculate_rms_error_of_two_corner_dataset(aligned_board, simulated_board)
        print(f"RMS Error of board corners: {rms_error}")


accuracy_tester = AccuracyTest()
accuracy_tester.run_accuracy_tester()
