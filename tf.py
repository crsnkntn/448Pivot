import os
import json
import ast
import pandas as pd
import numpy as np
from scipy.signal import find_peaks
from policies import *
from helper import *
from dnn import *

RUN_NAME = 'unique'
RUN_NOTES = 'these are notes, if needed'
DATASET_DIR = f'dataset_{RUN_NAME}'
csv_dir = 'threads'
wp_dir = 'win_probs'
drive_dir = 'drive_win_probs'

BINNING_POLICY = 'drive'
NORM_POLICY = 'standard'
CLASS_POLICY = 'ternary'


THRESHOLD = 0.03
RANDOM_STATE = 345
BATCH_SIZE = 32
DROPOUT_RATE = 0.2
DNN_LAYERS = [357, 468, 3]


PATIENCE = 3
MAX_EPOCHS = 20
LR = 0.001
GRAD_CLIP_NORM_MAX_NORM = 1.0

info = f'''
RUN_NAME: {RUN_NAME}
RUN_NOTES: {RUN_NOTES}
DATASET_DIR: {DATASET_DIR}
BINNING_POLICY: {BINNING_POLICY}
NORM_POLICY: {NORM_POLICY}
CLASS_POLICY: {CLASS_POLICY}

THRESHOLD: {THRESHOLD}
RANDOM_STATE: {RANDOM_STATE}
BATCH_SIZE: {BATCH_SIZE}
DROPOUT_RATE: {DROPOUT_RATE}
DNN_LAYERS: {DNN_LAYERS}

PATIENCE: {PATIENCE}
MAX_EPOCHS: {MAX_EPOCHS}
LEARNING_RATE: {LR}
GRAD_CLIP_NORM_MAX_NORM: {GRAD_CLIP_NORM_MAX_NORM}
'''

print(info)

if not os.path.exists(DATASET_DIR):
    os.makedirs(DATASET_DIR)

class_policy_func, n_classes = get_class_info(CLASS_POLICY)

# returns dataloaders
def load_existing_dataset(filename):
    features = []
    targets = []

    for filename in os.listdir(DATASET_DIR):
        with open(os.path.join(DATASET_DIR, filename), 'r') as json_file:
            data = json.load(json_file)

        for interval in data["game_datapoints"]:
            home_vals = list(interval['home_vals'].values())
            away_vals = list(interval['away_vals'].values())
            neut_vals = list(interval['neut_vals'].values())
            
            feature = np.concatenate((home_vals, away_vals, neut_vals), axis=None)
            features.append(feature)  # Append the combined features as one row

            # Store each target
            if abs(interval['wp_delta']) < THRESHOLD:
                target = 0
            elif interval['wp_delta'] > 0:
                target = 1  # Represents increase
            else:
                target = 2
            
            targets.append(target)

    features = torch.tensor(features, dtype=torch.float32)
    if torch.isnan(features).any():
        print("NaN values found in features, replacing with 0")
        features[torch.isnan(features)] = 0

    if torch.isinf(features).any():
        print("Infinite values found in features, replacing with 0")
        features[torch.isinf(features)] = 0

    # Normalize features
    mean = features.mean(dim=0)
    std = features.std(dim=0)
    features = (features - mean) / (std + 1e-6)  # Adding a small constant to avoid division by zero

    print(targets.count(0) / len(targets))
    print(targets.count(1) / len(targets))
    print(targets.count(2) / len(targets))

    # Convert lists to PyTorch tensors
    targets = torch.tensor(targets, dtype=torch.long)
    # Splitting data into training, validation, and test sets
    X_train_val, X_test, y_train_val, y_test = train_test_split(features, targets, test_size=0.2, random_state=RANDOM_STATE)
    X_train, X_val, y_train, y_val = train_test_split(X_train_val, y_train_val, test_size=0.2, random_state=RANDOM_STATE)

    # Creating PyTorch datasets
    train_dataset = TensorDataset(X_train, y_train)
    val_dataset = TensorDataset(X_val, y_val)
    test_dataset = TensorDataset(X_test, y_test)

    # Create a WeightedRandomSampler for the training set to handle class imbalance
    class_sample_count = torch.tensor([(y_train == t).sum() for t in torch.unique(y_train, sorted=True)])
    weight = 1. / class_sample_count.float()
    samples_weight = torch.tensor([weight[t] for t in y_train])
    sampler = WeightedRandomSampler(weights=samples_weight, num_samples=len(samples_weight), replacement=True)

    # Creating data loaders
    train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, sampler=sampler)
    val_loader = DataLoader(val_dataset, batch_size=BATCH_SIZE, shuffle=False)
    test_loader = DataLoader(test_dataset, batch_size=BATCH_SIZE, shuffle=False)

    return train_loader, val_loader, test_loader


def make_datasets():
    binning_policy_func = get_binning_policy(BINNING_POLICY)
    norm_policy_func = get_norm_policy(NORM_POLICY)

    # These are the columns that we care about
    columns_to_keep = ['created_utc', 'labels'] + feature_columns
    all_game_datapoints = []

    # Loop through each thread
    for filename in os.listdir(wp_dir):
        if not filename.endswith('.json'):
            continue

        file_path = os.path.join(csv_dir, filename[:-5] + ".csv")
        if not os.path.exists(file_path):
            continue

        hometeam, awayteam = filename[:-5].split('_')
        intervals = binning_policy_func(filename)

        # Iterate through the intervals
        for start_utc, end_utc, wp_delta in intervals:
            try:
                home_comments, away_comments, neut_comments = separate_by_affiliation(file_path, columns_to_keep, start_utc, end_utc, hometeam, awayteam)
                datapoint = {
                    "start_utc": start_utc,
                    "end_utc": end_utc,
                    "home_vals": average_sentiments(home_comments),
                    "away_vals": average_sentiments(away_comments),
                    "neut_vals": average_sentiments(neut_comments),
                    "wp_delta": wp_delta
                }
                all_game_datapoints.append(datapoint)
            except pd.errors.EmptyDataError:
                continue

    # Data processing for creating dataloaders
    THRESHOLD = 0.03
    RANDOM_STATE = 690
    BATCH_SIZE = 32
    features = []
    targets = []

    for interval in all_game_datapoints:
        home_vals = list(interval['home_vals'].values())
        away_vals = list(interval['away_vals'].values())
        neut_vals = list(interval['neut_vals'].values())
        
        feature = np.concatenate((home_vals, away_vals, neut_vals), axis=None)
        features.append(feature)  # Append the combined features as one row

        # Store each target
        targets.append(class_policy_func(interval['wp_delta'], THRESHOLD))

    features = torch.tensor(features, dtype=torch.float32)
    targets = torch.tensor(targets, dtype=torch.long)

    # Handle potential NaN or infinite values in features
    if torch.isnan(features).any():
        features[torch.isnan(features)] = 0
    if torch.isinf(features).any():
        features[torch.isinf(features)] = 0

    # Normalize features
    mean = features.mean(dim=0)
    std = features.std(dim=0)
    features = (features - mean) / (std + 1e-6)



    # Splitting data into training, validation, and test sets
    X_train_val, X_test, y_train_val, y_test = train_test_split(features, targets, test_size=0.2, random_state=RANDOM_STATE)
    X_train, X_val, y_train, y_val = train_test_split(X_train_val, y_train_val, test_size=0.2, random_state=RANDOM_STATE)

    # Creating PyTorch datasets
    train_dataset = TensorDataset(X_train, y_train)
    val_dataset = TensorDataset(X_val, y_val)
    test_dataset = TensorDataset(X_test, y_test)

    # Create a WeightedRandomSampler for the training set to handle class imbalance
    class_sample_count = torch.tensor([(y_train == t).sum() for t in torch.unique(y_train, sorted=True)])
    weight = 1. / class_sample_count.float()
    samples_weight = torch.tensor([weight[t] for t in y_train])
    sampler = WeightedRandomSampler(weights=samples_weight, num_samples=len(samples_weight), replacement=True)

    # Creating data loaders
    train_loader = DataLoader(train_dataset, batch_size=BATCH_SIZE, sampler=sampler)
    val_loader = DataLoader(val_dataset, batch_size=BATCH_SIZE, shuffle=False)
    test_loader = DataLoader(test_dataset, batch_size=BATCH_SIZE, shuffle=False)

    return train_loader, val_loader, test_loader


def make_model():
    return DNN(DNN_LAYERS, DROPOUT_RATE)


def train_model(model, train_loader, val_loader):
    # Loss and optimizer
    criterion = nn.CrossEntropyLoss()
    optimizer = optim.Adam(model.parameters(), lr=LR)
    torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=GRAD_CLIP_NORM_MAX_NORM)

    best_loss = float('inf')
    patience_counter = 0

    for epoch in range(MAX_EPOCHS):
        model.train()
        running_loss = 0.0
        correct_predictions = 0
        total_predictions = 0

        # Training phase
        for inputs, labels in train_loader:
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, labels)
            loss.backward()
            optimizer.step()
            running_loss += loss.item() * inputs.size(0)
            _, predicted = torch.max(outputs, 1)
            correct_predictions += (predicted == labels).sum().item()
            total_predictions += labels.size(0)

        epoch_loss = running_loss / len(train_loader.dataset)
        epoch_acc = correct_predictions / total_predictions
        print(f"Epoch {epoch+1}/{MAX_EPOCHS} - Loss: {epoch_loss:.4f}, Acc: {epoch_acc:.4f}")

        # Validation phase
        model.eval()  # Evaluation mode
        val_loss = 0.0
        with torch.no_grad():
            for inputs, labels in val_loader:
                outputs = model(inputs)
                loss = criterion(outputs, labels)
                val_loss += loss.item() * inputs.size(0)

        val_loss /= len(val_loader.dataset)
        print(f"Validation Loss: {val_loss:.4f}")

        # Early stopping check
        if val_loss < best_loss:
            best_loss = val_loss
            patience_counter = 0  # reset patience
        else:
            patience_counter += 1  # decrement patience

        if patience_counter >= PATIENCE:
            print("Early stopping triggered")
            break


def eval_model(model, test_loader):
    model.eval()
    total = 0
    correct = 0
    all_preds = []
    all_labels = []
    with torch.no_grad():
        for inputs, labels in test_loader:
            outputs = model(inputs)
            _, predicted = torch.max(outputs.data, 1)
            total += labels.size(0)
            correct += (predicted == labels).sum().item()
            all_preds.extend(predicted.numpy())
            all_labels.extend(labels.numpy())

    accuracy = correct / total
    print(f'Test Accuracy: {accuracy}')

    # Confusion Matrix
    cm = confusion_matrix(all_labels, all_preds)
    print("Confusion Matrix:")
    print(cm)

    return accuracy


if __name__ == "__main__":
    avg_sum = 0
    seeds = [748, 87209, 679, 222, 62, 987, 3, 56, 745, 673]
    for i in seeds:
        RANDOM_STATE = i
        train_loader, val_loader, test_loader = make_datasets()

        model = make_model()

        train_model(model, train_loader, val_loader)

        avg_sum += eval_model(model, test_loader)

    print(f"Avg perf: {avg_sum / len(seeds)}")