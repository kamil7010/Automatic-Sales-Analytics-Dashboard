const dropzone = document.getElementById('dropzone');
const fileInput = document.getElementById('fileInput');
const dropzoneText = document.getElementById('dropzoneText');

if (dropzone && fileInput) {
    fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
            dropzoneText.textContent = fileInput.files[0].name;
        }
    });

    ['dragenter', 'dragover'].forEach(evt =>
        dropzone.addEventListener(evt, (e) => {
            e.preventDefault();
            dropzone.classList.add('dragover');
        })
    );

    ['dragleave', 'drop'].forEach(evt =>
        dropzone.addEventListener(evt, (e) => {
            e.preventDefault();
            dropzone.classList.remove('dragover');
        })
    );

    dropzone.addEventListener('drop', (e) => {
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            fileInput.files = files;
            dropzoneText.textContent = files[0].name;
        }
    });
}