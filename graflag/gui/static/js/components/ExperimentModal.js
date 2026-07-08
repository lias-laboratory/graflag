export default {
    name: 'ExperimentModal',
    props: {
        show: Boolean,
        content: String
    },
    emits: ['close'],
    methods: {
        handleClose() {
            this.$emit('close');
        }
    },
    template: `
        <div v-if="show" class="modal" @click="handleClose" style="display: flex;">
            <div class="modal-content" @click.stop>
                <span class="modal-close" @click="handleClose" style="font-size: 2rem; line-height: 0.5; cursor: pointer;">&times;</span>
                <div v-html="content"></div>
            </div>
        </div>
    `
};
